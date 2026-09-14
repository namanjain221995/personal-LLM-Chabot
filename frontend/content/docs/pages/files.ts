import type { DocPage } from '../types';
import { API_BASE_URL, EXAMPLE_STATUS, MODEL_ID } from '../samples';

// 2026-09-13, Files API programme. /v1/files and /v1/uploads were built as
// new modules (orchestrator/app/apifiles, orchestrator/app/publicapi/files)
// and are NOT yet mounted on the running router. This page, uploads.ts and
// fileInputs.ts describe the API as it behaves once they are, and are held
// back from the site by FILES_API_PUBLISHED below until that is true.

/**
 * WHETHER THE FILES PAGES ARE PUBLISHED — the Files API's honesty switch.
 *
 * Every page on this site carries the shared notice that "the /v1 routes these
 * examples call are live", and tests/docs-site.test.tsx refuses any /v1 route
 * that CONTRACT §7 does not list. Neither is true of /v1/files and /v1/uploads
 * today: the handlers exist, the router does not register them, and the
 * contract's route table does not name them. So content/docs/index.ts lists
 * these three pages only while this is `true`.
 *
 * tests/docs-files.test.tsx ties the value to four things, and refuses a
 * `true` while any is missing and a `false` once all four hold:
 *
 *  1. CONTRACT §7 lists `POST /v1/files`;
 *  2. the orchestrator registers the file routes FULLY WIRED — read as code
 *     from publicapi/router.py or main.py (comments, docstrings and functions
 *     nobody calls do not count): a registration that runs, with a
 *     FilesDependencies that sets processing_view, derived, events and
 *     purge_blob, directly or through routes.router_dependencies(). Without
 *     them the router serves 11 of the 14 routes, a File object with no
 *     derived names and unfiltered facts, and a DELETE that does not stop
 *     processing;
 *  3. the PUBLIC EDGE carries what the pages describe. Either
 *     app/v1/[[...path]]/route.ts — exercised as a handler, not read — takes
 *     PUT and DELETE, lets a 64 MiB part and a single upload through, forwards
 *     Range, If-None-Match, X-Part-SHA256 and Content-Digest, and relays
 *     Content-Disposition, Content-Security-Policy, ETag, Accept-Ranges,
 *     Content-Range and x-should-retry; or a compose file builds the /v1
 *     gateway, whose lists already carry them. Without this, a resumable
 *     upload, a DELETE and a large file fail through the public URL, and the
 *     download headers and x-should-retry the pages promise never arrive;
 *  4. FILES_EDGE_PROBE below records a passing run through the public URL.
 *
 * Nobody has to remember.
 */
export const FILES_API_PUBLISHED: boolean = false;

/** What a measured run through the public URL found (see FILES_EDGE_PROBE). */
export interface FilesEdgeProbe {
  /** The day of the run, `YYYY-MM-DD`. */
  ranOn: string;
  /** A 64 MiB multipart part was accepted through both the Python and the Node SDK. */
  fullSizePartAccepted: boolean;
  /** A single upload sent as a stream (chunked, no Content-Length) was accepted. */
  chunkedCreateAccepted: boolean;
  /** A 64 MiB part refused early (a bad key, over the cap) reached both SDKs as the JSON envelope. */
  envelopeOnEarlyRefusal: boolean;
  /** What a 16 MiB part sent at about 1 Mbit/s, for over 100 seconds, received. */
  slowPartStatus: 200 | 408 | 524;
}

/**
 * THE PUBLIC EDGE PROBE — the fourth condition of FILES_API_PUBLISHED.
 *
 * Several things these pages state depend on the network edge in front of the
 * API, and none of them has been measured through the public URL: that a
 * 64 MiB part gets through, the edge's per-request byte ceiling and its HTML
 * 413, whether an early refusal reaches an SDK as JSON, and what cuts off a
 * slow part. The Files design makes a small run through the public URL a
 * mandatory gate before the docs publish part sizes. Record that run here,
 * by hand, when it has been made; until then this is null and the pages stay
 * unpublished. The pages already tell a reader to drop to 16 MiB parts after a
 * 408 or a 524, whichever the run finds.
 */
export const FILES_EDGE_PROBE: FilesEdgeProbe | null = null;

/** A file id in the documented shape (`file-` + 24 hex). Never a real one. */
export const EXAMPLE_FILE_ID = 'file-6f1c2a9e0b7d4c3a8e5f1b2c';
/** An upload id in the documented shape (`upload_` + 24 hex). */
export const EXAMPLE_UPLOAD_ID = 'upload_8a0d3e5b7c9f1a2b4c6d8e0f';
/** A part id in the documented shape (`part_` + 24 hex). */
export const EXAMPLE_PART_ID = 'part_3c5e7a9b1d2f4a6c8e0b2d4f';
/** The sha256 every sample file object carries (64 hex, not of any real file). */
export const EXAMPLE_SHA256 = '9b1c4e7a2d5f8c0b3e6a9d2c5f8b1e4a7d0c3f6b9e2a5d8c1f4b7e0a3d6c9f2b';

/** One number per ceiling, in bytes, so a page cannot say two things. */
export const FILE_LIMITS = {
  singleMaxBytes: 67_108_864,
  singleMaxBodyBytes: 68_157_440,
  partMaxBytes: 67_108_864,
  maxParts: 10_000,
  uploadMaxBytes: 107_374_182_400,
  listMaxLimit: 10_000,
  expiresMinSeconds: 3_600,
  expiresMaxSeconds: 2_592_000,
  jsonMaxBytes: 1_048_576,
  pdfMaxBytes: 1_073_741_824,
  pdfMaxPages: 10_000,
  ocrPageBudget: 1_000,
  officeMaxBytes: 536_870_912,
  xlsxMaxBytes: 268_435_456,
  textMaxBytes: 1_073_741_824,
  imageMaxBytes: 67_108_864,
  imageMaxPixels: 89_478_485,
  mediaMaxSeconds: 14_400,
  indexMaxChunks: 50_000,
  tombstoneDays: 30,
} as const;

export const files: DocPage = {
  slug: 'files',
  title: 'Files',
  summary:
    'Upload a file once, follow its processing, download it or what was ' +
    'extracted from it, and use its file_id as model input until you delete it.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
A file is uploaded once and then referred to by its id. The service reads it
as soon as it arrives — text out of documents, OCR for scanned pages, a
transcript for audio and video, resized copies of images — so that a request
naming the file later starts from the work already done.

| Route | Scope | What it does |
| --- | --- | --- |
| \`POST /v1/files\` | \`files.write\` | Upload a file of up to 64 MiB in one request. |
| \`GET /v1/files\` | \`files.read\` | List this project's files. |
| \`GET /v1/files/{file_id}\` | \`files.read\` | Read one file object, including its processing state. |
| \`GET /v1/files/{file_id}/content\` | \`files.read\` | Download the original bytes. |
| \`GET /v1/files/{file_id}/events\` | \`files.read\` | Follow processing as server-sent events. |
| \`GET /v1/files/{file_id}/derived\` | \`files.read\` | List what processing produced. |
| \`GET /v1/files/{file_id}/derived/{name}\` | \`files.read\` | Download one of those: a transcript, the extracted text, a profile. |
| \`DELETE /v1/files/{file_id}\` | \`files.write\` | Delete the file and everything derived from it. |

Anything larger than 64 MiB — up to 100 GiB — goes through
[uploads](/docs/uploads), which end in the same file object. How a file
becomes part of a prompt is on [files as model input](/docs/file-inputs).

**Scopes.** \`files.write\` uploads and deletes; \`files.read\` lists,
downloads and lets a key use a file as model input — a model can be asked to
repeat a file, so using one is reading it. A key keeps the scopes it was
created with: a key made before the Files API has neither, and answers
\`403 insufficient_scope\` on these routes. Create a new key with the scopes
you need — see [authentication](/docs/authentication#scopes).

**No \`Idempotency-Key\`.** Every file and upload route refuses the header
with a \`400\` naming it. Nothing here needs one: an upload's parts are
idempotent by their part number, \`complete\` and \`cancel\` are idempotent by
the upload's state, and uploading the same bytes twice gives a second file id
over the one stored copy.

## Upload a small file

The request is \`multipart/form-data\` with the file and its purpose:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/files \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -F purpose=user_data \\
  -F file=@q3-report.pdf
~~~

| Field | Required | Rule |
| --- | --- | --- |
| \`file\` | yes | One file of at most 67,108,864 bytes (64 MiB). The name is kept (at most 255 characters, path removed). What the file is comes from its bytes; the name only breaks ties between text formats, as [kinds](#kinds-and-what-processing-does) explains. |
| \`purpose\` | yes | \`user_data\`. \`assistants\` and \`vision\` are accepted and stored as given. \`batch\`, \`fine-tune\` and \`evals\` are refused: there is no batch, fine-tuning or evaluation product here. |
| \`expires_after[anchor]\` | with \`seconds\` | \`created_at\`, the only anchor. |
| \`expires_after[seconds]\` | with \`anchor\` | 3,600 to 2,592,000 (one hour to 30 days). Leave both out and the file is kept until you delete it. |

Any other field is a \`400\` naming it. The answer is the
[file object](#the-file-object), usually with \`status: "uploaded"\` — or
\`processed\` at once, when this project has already uploaded and processed
exactly these bytes.

The same upload with the \`openai\` Python package, pointed at this API:

~~~python
import os
from pathlib import Path
from openai import OpenAI

client = OpenAI(base_url="${API_BASE_URL}", api_key=os.environ["TECHSARA_API_KEY"])

uploaded = client.files.create(file=Path("q3-report.pdf"), purpose="user_data")
ready = client.files.wait_for_processing(uploaded.id, poll_interval=5, max_wait_seconds=1800)
print(ready.status, ready.id)          # processed file-…
~~~

And with the \`openai\` Node package:

~~~typescript
import fs from "node:fs";
import OpenAI, { toFile } from "openai";

const client = new OpenAI({ baseURL: "${API_BASE_URL}", apiKey: process.env.TECHSARA_API_KEY });

// A Buffer, not fs.createReadStream: the SDK never retries a streamed body,
// so a dropped connection would surface as an error instead of a retry.
const uploaded = await client.files.create({
  file: await toFile(await fs.promises.readFile("q3-report.pdf"), "q3-report.pdf"),
  purpose: "user_data",
});
const ready = await client.files.waitForProcessing(uploaded.id, { pollInterval: 5000, maxWait: 30 * 60 * 1000 });
console.log(ready.status, ready.id);
~~~

The Python package reads a \`Path\` into memory before sending it, which is
one reason the single-request ceiling is 64 MiB. For anything bigger use
\`client.uploads.upload_file_chunked\`, on [uploads](/docs/uploads).

## The file object

~~~json
{
  "id": "${EXAMPLE_FILE_ID}",
  "object": "file",
  "bytes": 48213904,
  "created_at": 1789300000,
  "filename": "q3-report.pdf",
  "purpose": "user_data",
  "status": "processed",
  "status_details": null,
  "expires_at": null,
  "sha256": "${EXAMPLE_SHA256}",
  "mime_type": "application/pdf",
  "processing": {
    "state": "processed",
    "kind": "pdf",
    "stage": "finalize",
    "step": 6,
    "total_steps": 6,
    "percent": 100,
    "stages": [
      { "name": "sniff", "status": "done" },
      { "name": "text", "status": "done" },
      { "name": "ocr", "status": "done" },
      { "name": "chunk", "status": "done" },
      { "name": "index", "status": "done" },
      { "name": "finalize", "status": "done" }
    ],
    "queue_position": null,
    "waited_for_capacity_s": 0.0,
    "started_at": 1789300002,
    "finished_at": 1789300071,
    "error": null,
    "facts": {
      "pages": 1000, "text_pages": 988, "ocr_pages": 12, "ocr_skipped_pages": 0,
      "chars": 1756000, "estimated_tokens": 585333,
      "chunks": 1402, "chunks_indexed": 1402, "index_truncated": false
    },
    "derived": ["pages.json", "text.txt"]
  }
}
~~~

| Field | Meaning |
| --- | --- |
| \`status\` | \`uploaded\` while the file is being assembled, queued or processed; \`processed\` when it is ready; \`error\` when processing failed. These are the values the SDKs' wait helpers understand. |
| \`status_details\` | \`null\`, or one fixed sentence saying why the file failed — see [errors](#errors). Never an internal message. |
| \`expires_at\` | When the file is deleted automatically, or \`null\` for kept until you delete it. |
| \`sha256\` | The digest of the original bytes. Also the \`ETag\` of the download. |
| \`mime_type\` | What the bytes **are**, read from the bytes themselves. Only for text do the name's extension and the type your client sent help decide the format — see [kinds](#kinds-and-what-processing-does). \`null\` until the bytes are assembled. |
| \`processing.state\` | \`queued\`, \`processing\`, \`processed\` or \`failed\`. |
| \`processing.kind\` | What the file was read as — see [kinds](#kinds-and-what-processing-does). |
| \`processing.stage\`, \`step\`, \`total_steps\`, \`percent\` | Where processing is. Stage names are fixed per kind, so a progress bar can be drawn from them. |
| \`processing.stages\` | Every stage of this kind, each \`pending\`, \`running\` (with a \`percent\` when known), \`done\` or \`failed\`. |
| \`processing.waited_for_capacity_s\` | Seconds this file's processing has waited for a shared engine — see [how long it takes](#how-long-processing-takes). |
| \`processing.error\` | \`{"code", "message"}\` when \`state\` is \`failed\`. |
| \`processing.facts\` | Numbers about the content: pages, duration, rows. Only the keys listed under [kinds](#kinds-and-what-processing-does) ever appear. |
| \`processing.derived\` | The names you can download from \`GET /v1/files/{file_id}/derived/{name}\`. Empty until processed. |

A file that arrived through an [upload](/docs/uploads) and is still being put
together from its parts has \`status: "uploaded"\`, \`processing.stage:
"assemble"\` with a \`percent\`, and \`mime_type: null\`.

## Kinds and what processing does

What a file *is* is decided from its bytes — magic numbers, the members of a
zip container, the shape of the text. The name and the type your client sent
only break ties between text formats: a \`.csv\` or \`.tsv\` name, or
\`text/csv\`, reads any text as a table; \`.jsonl\` reads JSON Lines; a
JSON object named \`.json\` (or sent as \`application/json\`) is read as
text; \`.md\` is Markdown; and a \`.txt\` or \`.md\` name keeps
comma-separated text from being read as a table. The kind decides the stages:

| Kind | Files | Stages |
| --- | --- | --- |
| \`pdf\` | PDF | \`sniff\`, \`text\`, \`ocr\`, \`chunk\`, \`index\`, \`finalize\` |
| \`document\` | Word (.docx) | \`sniff\`, \`text\`, \`chunk\`, \`index\`, \`finalize\` |
| \`presentation\` | PowerPoint (.pptx), slide text and speaker notes | \`sniff\`, \`text\`, \`chunk\`, \`index\`, \`finalize\` |
| \`text\` | Plain text, Markdown, source code, a JSON object | \`sniff\`, \`text\`, \`chunk\`, \`index\`, \`finalize\` |
| \`html\` | HTML, read as its readable text | \`sniff\`, \`text\`, \`chunk\`, \`index\`, \`finalize\` |
| \`spreadsheet\` | Excel (.xlsx), every sheet | \`sniff\`, \`sheets\`, \`profile\`, \`chunk\`, \`index\`, \`finalize\` |
| \`tabular\` | CSV, TSV, Parquet, JSON Lines, a JSON array of objects | \`sniff\`, \`sheets\`, \`profile\`, \`chunk\`, \`index\`, \`finalize\` |
| \`image\` | PNG, JPEG, WebP, GIF and TIFF (first frame), BMP | \`sniff\`, \`decode\`, \`variants\`, \`finalize\` |
| \`audio\` | MP3, M4A, WAV, FLAC, Ogg and the other common containers | \`sniff\`, \`probe\`, \`audio\`, \`transcript\`, \`fusion\`, \`artifacts\`, \`index\`, \`finalize\` |
| \`video\` | MP4, MOV, WebM, MKV and the other common containers | \`sniff\`, \`probe\`, \`audio\`, \`transcript\`, \`frames\`, \`ocr\`, \`vision\`, \`fusion\`, \`artifacts\`, \`index\`, \`finalize\` |
| \`unsupported\` | Anything else | \`sniff\`, \`finalize\` |

In words:

* **Documents** (\`pdf\`, \`document\`, \`presentation\`, \`text\`, \`html\`) have
  their text read page by page — a DOCX, which has no pages, in sections of
  about 3,000 characters — then cut into chunks of at most 1,500 characters
  and indexed for search. A PDF page with fewer than 200 characters of text is
  treated as scanned and read with OCR by \`techsara-ocr\`, up to 1,000 pages
  per file.
* **Tables** (\`spreadsheet\`, \`tabular\`) get a profile — columns, types,
  counts — and their rows are cut into blocks of 200 with the header repeated.
* **Images** are decoded, turned upright and stripped of their metadata, and
  stored at three sizes (896, 1,600 and 2,560 pixels on the long edge) for the
  vision models.
* **Audio and video** are transcribed with \`techsara-whisper\`, summarised
  and divided into chapters; a video's frames are also sampled, their on-screen
  text read and their content described. Then the transcript and the screen
  text are indexed.

**Unsupported**, and reported as \`status: "error"\` with the code
\`unsupported_file\` rather than as a silent success: zip and tar archives,
the old binary Office formats (.doc, .xls, .ppt), OpenDocument files,
HEIC/HEIF images, macro-enabled Office files (.docm, .xlsm, .pptm and the
like), and media playlists. The bytes of an unsupported file can still be
downloaded.

The \`facts\` each kind may report:

| Kind | \`processing.facts\` |
| --- | --- |
| \`pdf\` | \`pages\`, \`text_pages\`, \`ocr_pages\`, \`ocr_skipped_pages\`, \`chars\`, \`estimated_tokens\` |
| \`document\`, \`text\`, \`html\` | \`sections\`, \`chars\`, \`estimated_tokens\` |
| \`presentation\` | \`slides\`, \`chars\`, \`estimated_tokens\` |
| \`spreadsheet\` | \`sheets\`, \`rows\`, \`columns\`, \`chars\`, \`estimated_tokens\` |
| \`tabular\` | \`rows\`, \`columns\`, \`chars\`, \`estimated_tokens\` |
| \`image\` | \`width\`, \`height\`, \`format\` |
| \`audio\` | \`duration_s\`, \`has_audio\`, \`has_video\`, \`language\`, \`speech_fraction\` |
| \`video\` | \`duration_s\`, \`has_audio\`, \`has_video\`, \`width\`, \`height\`, \`language\`, \`speech_fraction\` |

Every indexed kind adds \`chunks\`, \`chunks_indexed\` and
\`index_truncated\`: at most 50,000 chunks of one file are indexed, and
\`index_truncated: true\` says a very long file passed that point — the rest
is still downloadable and still used when you ask for the file in \`full\`
mode, but retrieval cannot find it.

**OCR is best effort.** \`ocr_skipped_pages\` counts scanned pages that were
not sent to OCR: those past the 1,000-page budget, or every scanned page when
OCR is not available on the service. A page OCR tried and could not read keeps
its text layer too, and is not counted in \`facts\`. None of this fails the
file: a scanned PDF can end \`processed\` with some pages holding only their
(nearly empty) text layer, and a question about those pages has nothing to go
on. \`pages.json\` in [derived data](#derived-data) is the per-page answer —
a page read by OCR has \`"source": "ocr"\`.

## Wait for processing

Three ways, in order of how much they cost you:

1. **Poll** \`GET /v1/files/{file_id}\` until \`status\` is \`processed\` or
   \`error\`. The SDK helpers above do exactly this. Both give up after 30
   minutes unless told otherwise, which is too short for a long video: pass
   \`max_wait_seconds\` (Python) or \`maxWait\` in milliseconds (Node) sized to
   the file.
2. **Follow the events** of \`GET /v1/files/{file_id}/events\`, below.
3. **Be told**: an endpoint subscribed to the \`file.processed\` and
   \`file.failed\` [webhooks](/docs/webhooks) is sent one when processing of
   a file's bytes ends. **A file whose bytes this project had already
   processed gets no webhook**: it is \`processed\` (or \`error\`) the moment
   it is created — or, from an upload, the moment its parts are assembled —
   because there is no processing left to end. So read the file first, and
   wait for a webhook only while its \`status\` is \`uploaded\` and its
   \`processing.stage\` is past \`assemble\`; otherwise act on the \`status\`
   you read. A file in \`error\` with \`processing_unavailable\` or
   \`internal_error\` can still get a webhook: when the same bytes are
   [uploaded again](#retrying-a-failed-file), processing starts over and every
   file holding them is sent \`file.processed\` or \`file.failed\` when it
   ends — even one that was already sent \`file.failed\`.

You do not have to wait at all before *using* a file: a request that names a
file still being processed waits for it — see
[readiness](/docs/file-inputs#a-file-that-is-still-processing).

### How long processing takes

| File | Typically |
| --- | --- |
| A document or spreadsheet with a text layer | Seconds, then a little longer to index a long one. |
| A scanned PDF | Each thin page is read by OCR, so minutes for hundreds of pages. |
| An image | Seconds. |
| Audio or video | Several times faster than real time when the service is quiet, about twice real time when it is busy: a 3-hour recording takes roughly 25 to 90 minutes. |

Transcription, OCR and indexing run on engines shared with the rest of the
service, so a file can wait for them.
\`processing.waited_for_capacity_s\` shows how much of the time was waiting.
A file that cannot reach an engine is retried later, with the stages already
finished kept; after several failed attempts it ends \`error\` with
\`processing_unavailable\`. OCR is the exception: on the last attempt a PDF
ends \`processed\` with the pages OCR managed to read, and the rest keep their
text layer — see [the facts](#kinds-and-what-processing-does). A service
restart does not lose progress either: processing resumes at the first
unfinished stage.

## Progress events

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -N ${API_BASE_URL}/files/${EXAMPLE_FILE_ID}/events \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

~~~text
event: file.processing
data: {"type":"file.processing","sequence_number":1,"data":{"id":"${EXAMPLE_FILE_ID}","object":"file","status":"uploaded","processing":{"state":"processing","kind":"video","stage":"transcript","step":4,"total_steps":11,"percent":31}}}

: ping

event: file.processed
data: {"type":"file.processed","sequence_number":2,"data":{"id":"${EXAMPLE_FILE_ID}","object":"file","status":"processed","processing":{"state":"processed","kind":"video","stage":"finalize","step":11,"total_steps":11,"percent":100}}}
~~~

The \`data\` of every event is the whole [file object](#the-file-object);
it is shortened above. The rules:

* \`file.processing\` is sent when the stage or the percent changes, at most
  once a second.
* Exactly one terminal event ends the stream: \`file.processed\`, or
  \`file.failed\` for a file that failed — or was **deleted** while you were
  watching, in which case its \`status\` is \`"deleted"\`.
* A file that has already finished gets its terminal event at once.
* \`sequence_number\` starts at 1 and goes up by one per event.
* After 15 seconds with nothing to say, a \`: ping\` comment keeps the
  connection alive. There is no server-side deadline: the stream stays open
  for as long as processing takes.

## Derived data

What processing produced can be listed and downloaded:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/files/${EXAMPLE_FILE_ID}/derived \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"

curl ${API_BASE_URL}/files/${EXAMPLE_FILE_ID}/derived/transcript.vtt \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" -o standup.vtt
~~~

~~~json
{
  "object": "list",
  "data": [
    { "name": "transcript.vtt", "bytes": 48211, "content_type": "text/vtt" },
    { "name": "transcript.txt", "bytes": 39904, "content_type": "text/plain; charset=utf-8" }
  ]
}
~~~

| Kind | Names |
| --- | --- |
| \`pdf\`, \`document\`, \`presentation\`, \`text\`, \`html\` | \`text.txt\`, \`pages.json\` |
| \`spreadsheet\` | \`profile.json\`, \`text.txt\`, \`sheet-1.csv\`, \`sheet-2.csv\`, … |
| \`tabular\` | \`profile.json\`, \`text.txt\` |
| \`image\` | \`image.png\` |
| \`audio\` | \`transcript.txt\`, \`transcript.srt\`, \`transcript.vtt\`, \`transcript.json\`, \`summary.md\` |
| \`video\` | the audio names, plus \`screen_text.txt\` and \`screen_text.json\` |

\`text.txt\` marks where each unit starts — \`--- Page 12 ---\`,
\`--- Section 3 ---\`, \`--- Slide 7 ---\`, \`--- Rows 201-400 (Sales) ---\` —
the same labels the model is shown. \`pages.json\` is
\`[{"page", "text", "source"}]\`, where \`source\` says whether a page's text
came from the text layer or from OCR. Only the names in this table exist;
any other name is \`404 file_not_found\` with \`param: "name"\`.

Derived data exists only for a file whose \`status\` is \`processed\`.
Asking while it is still \`uploaded\` — its parts being assembled, or its
processing queued or running — is \`409 file_not_ready\` with
\`Retry-After: 5\`, which both SDKs retry on their own. Asking for a file
whose \`status\` is \`error\` is \`400 invalid_request_error\` with
\`param: "file_id"\` and the message "This file has no derived data: "
followed by the file's own \`status_details\`. It is not worth retrying:
nothing changes until the bytes are
[uploaded again](#retrying-a-failed-file).

## Download the original

\`GET /v1/files/{file_id}/content\` streams the bytes exactly as uploaded:

| Request | Answer |
| --- | --- |
| No \`Range\` | \`200\` with the whole file. |
| \`Range: bytes=0-1048575\` (one range) | \`206\` with \`Content-Range\`. |
| Several ranges | \`200\` with the whole file. |
| A range past the end | \`416\` with \`Content-Range: bytes */<size>\`. |
| \`If-None-Match: "<sha256>"\` matching the file | \`304\`, no body. |

Every download is \`Content-Type: application/octet-stream\` and
\`Content-Disposition: attachment\`, with \`ETag\` (the sha256),
\`Accept-Ranges: bytes\`, \`X-Content-Type-Options: nosniff\`, a sandboxing
\`Content-Security-Policy\` and \`Cache-Control: private, no-store\`. A file
is served for saving, never rendered — an uploaded HTML page or SVG cannot run
in anybody's browser from this origin.

A [derived](#derived-data) download carries the same \`Content-Disposition\`,
\`Accept-Ranges\`, \`X-Content-Type-Options\`, \`Content-Security-Policy\` and
\`Cache-Control\`, with its own type — text, Markdown, JSON, CSV, subtitles or
PNG — and answers a \`Range\` the same way. It has **no \`ETag\`**, so
\`If-None-Match\` never gets a \`304\` there.

A file whose parts are still being assembled answers \`409 file_not_ready\`
with \`Retry-After: 5\`. A file whose assembly failed — \`checksum_mismatch\`,
or \`internal_error\` while its parts were being joined — has no bytes to download:
\`400 invalid_request_error\` with \`param: "file_id"\` and one of two
sentences: "This file has no content: The assembled bytes did not match the
checksum you supplied." after a checksum mismatch, and "This file has no
content: The uploaded parts could not be assembled. Upload the file again."
after any other assembly failure. A file that failed *processing* still
downloads. For a large file, stream the download instead of holding it in
memory:

~~~python
with client.files.with_streaming_response.content("${EXAMPLE_FILE_ID}") as response:
    response.stream_to_file("standup.mp4")
~~~

## List files

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl "${API_BASE_URL}/files?limit=100&order=desc" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

~~~json
{
  "object": "list",
  "data": [ { "id": "${EXAMPLE_FILE_ID}", "object": "file", "status": "processed" } ],
  "has_more": false,
  "first_id": "${EXAMPLE_FILE_ID}",
  "last_id": "${EXAMPLE_FILE_ID}"
}
~~~

| Query | Rule |
| --- | --- |
| \`limit\` | 1 to 10,000; 10,000 when left out. |
| \`order\` | \`desc\` (newest first, the default) or \`asc\`. |
| \`after\` | The \`last_id\` of the previous page. |
| \`purpose\` | Only files with this purpose. |

Page until \`has_more\` is \`false\`; the SDKs' list iterators do. A loop that
deletes each file as it goes keeps working: \`after\` may name a file you have
just deleted. An \`after\` this project never had is a \`400\` with
\`param: "after"\`.

## Delete a file

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -X DELETE ${API_BASE_URL}/files/${EXAMPLE_FILE_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

~~~json
{ "id": "${EXAMPLE_FILE_ID}", "object": "file", "deleted": true }
~~~

A delete is immediate and physical. Within seconds the original, the extracted
text, the search index, transcripts and every other derived byte are gone, and
any processing still running for the file is stopped. What stays, for 30 days
and never returned by the API, is a record that the file existed: its name,
size and the times it was created and deleted.

* A second \`DELETE\` of the same id is \`404 file_not_found\`.
* If another file in the same project holds the **same bytes**, those bytes
  stay until that file is deleted too; each id is independent.
* A response that is already generating from the file is not interrupted —
  its input was built before the delete. A request that names the file after
  it is \`404\`.
* Uploading the same bytes again a moment after deleting them can meet the
  removal still in progress: \`503 storage_unavailable\` with
  \`Retry-After: 2\` and \`x-should-retry: true\`. Both SDKs retry that by
  themselves.

## Retention and isolation

**Kept until you delete it**, or until its \`expires_at\` when you set
\`expires_after\`; an expired file is deleted exactly as above, within about
ten minutes of that time. Nothing is deleted for age otherwise, and storage is
not metered as a quota. Revoking a key deletes nothing; a disabled project's
files are kept, and its keys are refused.

**One project, one set of files.** Every key and service account of a project
sees the project's files, and nothing else. A file id from another project,
a deleted or expired file's id, a malformed id and an id that never existed
all get the **same** \`404 file_not_found\`, byte for byte apart from the
request id — the API does not confirm that someone else's file exists.

Identical bytes uploaded twice **in the same project** are stored and processed
once, which is why a re-upload can be \`processed\` immediately. A re-upload
of bytes whose processing failed with \`unsupported_file\`, \`file_corrupt\`
or \`file_too_complex\` is \`error\` immediately, with the same code; after
\`processing_unavailable\` or \`internal_error\` it starts their processing
again (see [retrying a failed file](#retrying-a-failed-file)). That is
never true across projects: identical bytes in another project are stored and
processed from scratch, so how fast an upload finishes says nothing about what
other projects hold.

## Limits and why

None of these is a usage limit — there is no limit on how many files a
project keeps or how often it uploads. Each is a technical ceiling, and the
reason is next to it.

| Ceiling | Value | Why |
| --- | --- | --- |
| One \`POST /v1/files\` | 67,108,864 bytes (64 MiB) | The network edge in front of the API accepts at most 100,000,000 bytes per request; 64 MiB plus the multipart framing stays well inside it. Larger files use [uploads](/docs/uploads). |
| Request body of \`POST /v1/files\` | 68,157,440 bytes | The file plus 1 MiB for the multipart framing. |
| \`expires_after.seconds\` | 3,600 to 2,592,000 | One hour to 30 days, the range the SDK types accept. |
| \`limit\` on a list | 10,000 | The same maximum the SDKs expect. |
| PDF | 1 GiB, 10,000 pages | Bounds the memory a single document's reader may use and the size of its page table. |
| OCR per PDF | 1,000 pages | OCR is GPU time shared with the chat product; 1,000 pages is already tens of minutes of it. |
| DOCX and PPTX | 512 MiB | Office files are zip containers; compressed size is checked before anything is expanded. |
| XLSX | 256 MiB | Reading is row by row; this is several minutes of it. |
| Text, Markdown, HTML | 1 GiB | Beyond this, retrieval could index only the start of the file anyway. |
| CSV, TSV, Parquet, JSON Lines | 100 GiB, the upload ceiling | Tables are read as a stream, so size alone is not what stops them; reading time is. |
| Image | 64 MiB, 89,478,485 pixels | A small compressed image can expand to gigabytes of pixels; the models see at most 2,560 pixels on a side. |
| Audio and video | 4 hours (14,400 seconds) | Duration, not size, is what a transcription costs; bytes are bounded only by the upload ceiling. |
| Indexed chunks per file | 50,000 | Past this, \`index_truncated\` is set and the rest is not searchable. |
| Reading one file | 5 minutes for an image; 30 minutes for DOCX and PPTX; 1 hour for PDF, XLSX, text and HTML; 2 hours for CSV and the other tables; 8 GiB of memory for any of them | A guard against files built to exhaust the reader, sized far above what real files need: the text layer of a 10,000-page PDF reads in seconds, and about 27,000 spreadsheet rows are read each second. Audio and video are bounded by their duration instead. |

A file over a processing ceiling ends \`status: "error"\` with
\`file_too_complex\` and a sentence naming the ceiling — "The file exceeds a
processing ceiling: more than 10,000 pages.", or, for reading time and
memory, "The file exceeds a processing ceiling: it needs more time or memory
than the processing ceiling." Its bytes stay downloadable.

## Errors

The [envelope](/docs/errors#the-envelope) is the one every route uses.
These codes are the ones the file routes add:

| Code | Status | When | Retry? |
| --- | --- | --- | --- |
| \`file_not_found\` | 404 | The id is absent, malformed, deleted, expired or another project's; or a derived \`name\` that does not exist. | No |
| \`file_not_ready\` | 409 | The bytes are still being assembled; or derived data was asked for before processing finished. | Yes, after \`Retry-After\` |
| \`incomplete_body\` | 408 | The connection closed before the body was complete. Nothing was recorded. | Yes |
| \`request_too_large\` | 413 | The file is over 64 MiB. The message points to \`/v1/uploads\`. | No |
| \`storage_unavailable\` | 503 | The service is short of storage (\`Retry-After: 60\`, \`x-should-retry: false\`), or the same bytes are still being removed after a delete (\`Retry-After: 2\`, \`x-should-retry: true\`). | Only when \`x-should-retry\` is \`true\` |
| \`invalid_request_error\` | 400 | A missing or unknown field, a bad \`purpose\`, \`expires_after\`, \`limit\`, \`order\` or \`after\`, or an \`Idempotency-Key\`; the content of a file whose assembly failed; or the derived data of a file in \`error\`. | No |
| \`invalid_request_error\` | 416 | A \`Range\` past the end of the file. | No |

\`x-should-retry\` is a response header both SDKs obey before their own
rules: \`false\` stops an SDK from retrying a \`409\` or \`503\` that will not
change on the next attempt. Honour it in your own client too.

A body far past the ceiling — roughly 95 MB and above — can be refused by the
network edge before it reaches the API, with an HTML \`413\` page instead of
the JSON envelope. Treat any \`413\` as "use uploads".

When **processing** fails, the request that uploaded the file has long since
succeeded, so the failure is on the file object: \`status: "error"\`, the code
in \`processing.error.code\` and the sentence in \`status_details\`.

| \`processing.error.code\` | \`status_details\` |
| --- | --- |
| \`unsupported_file\` | This file type cannot be used as model input. |
| \`file_corrupt\` | The file could not be read; it may be damaged or encrypted. |
| \`file_too_complex\` | The file exceeds a processing ceiling: … (the ceiling, in words). |
| \`processing_unavailable\` | Processing could not reach a required service after several attempts. Upload the file again later. |
| \`internal_error\` | Something went wrong while processing this file. |
| \`checksum_mismatch\` | The assembled bytes did not match the checksum you supplied. |

A password-protected PDF is \`file_corrupt\`. \`checksum_mismatch\` happens
only to files made from an [upload](/docs/uploads#finish-the-upload).

Using a failed file in a request to \`${MODEL_ID}\` or any other model is a
\`400\` with the code's fixed sentence from this table — for
\`file_too_complex\`, "The file exceeds a processing ceiling." without the
ceiling itself — see [files as model input](/docs/file-inputs#errors).

### Retrying a failed file

\`unsupported_file\`, \`file_corrupt\` and \`file_too_complex\` are verdicts
on the bytes, and they are final: uploading the same bytes again, singly or
through an upload, gives a new file id that is already \`status: "error"\`
with the same code.

\`processing_unavailable\` and \`internal_error\` can pass, so for them
**upload the same bytes again** — there is nothing to delete first. Their
processing starts over from the first step it had not finished, and the new
file is \`uploaded\` while it runs. Every file in the project that holds
those bytes — every file with the same \`sha256\`, the one that failed
included — returns to \`uploaded\` with it and ends \`processed\` or
\`error\` together. So \`error\` with one of these two codes is not final:
read a file's \`status\` again before acting on an old failure.

One \`internal_error\` does not start over: the one recorded because
processing those bytes stopped unexpectedly several times in a row. Uploading
them again leaves every file that holds them in \`error\`.

A file that failed \`checksum_mismatch\` never had its bytes stored: upload
them again directly.
`.trim(),
};
