import type { DocPage } from '../types';
import { API_BASE_URL, EXAMPLE_STATUS } from '../samples';
import { EXAMPLE_FILE_ID, EXAMPLE_PART_ID, EXAMPLE_SHA256, EXAMPLE_UPLOAD_ID } from './files';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, Files API programme: the upload half, as built in
// orchestrator/app/publicapi/files/routes.py and apifiles/queue.py. Published
// with files.ts and fileInputs.ts under FILES_API_PUBLISHED (see files.ts).
// The two things that shaped the page: `complete` always answers 200 with a
// nested file (assembly is the file's first processing stage, so SDK waiters
// work at any size), and the first part fixes whether an upload is numbered
// or sequential, because mixing the two can silently reorder bytes.

/** Changing the part size is safe for the API and not for every script. */
const PART_SIZE_CHANGE = `Parts need not all be the
same size, but a script that works out part numbers from a fixed size, like
the [resumable one](#a-resumable-upload), must start a new upload to change it.`;

/**
 * One slow-link paragraph per state of the no-timeout release.
 *
 * Neither states a time limit or a minimum upload speed: what cuts off a slow
 * part through the public URL has not been measured (files.ts,
 * FILES_EDGE_PROBE), so both say what to do after a `408` or a `524`, which
 * is the rule whichever of the two the measurement finds.
 */
function slowLinks(noTimeout: boolean): string {
  if (noTimeout) {
    return `
**Slow links.** No clock on the service ends a part while its bytes keep
flowing, but a request body that sends nothing for 60 seconds is dropped, and
nothing of that part is recorded. The network edge in front of the API can
still cut off a very slow part, with a \`524\`. After a dropped part or a
\`524\`, use 16 MiB parts: a smaller part arrives sooner and loses less when a
connection is cut. ${PART_SIZE_CHANGE}`;
  }
  return `
**Slow links.** A part that takes too long to arrive can be cut off — with a
\`408\`, or with a \`524\` from the network edge in front of the API — and
nothing of it is recorded. After either, use 16 MiB parts: a smaller part
arrives sooner and loses less when a connection is cut. ${PART_SIZE_CHANGE}`;
}

export function uploadsPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  return {
    slug: 'uploads',
    title: 'Uploads',
    summary:
      'Files of up to 100 GiB, sent in parts of up to 64 MiB: resumable after ' +
      'a crash, checksummed, and finished by one call that returns the file at once.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: `
An upload is how a file larger than the 64 MiB of a single
[\`POST /v1/files\`](/docs/files#upload-a-small-file) arrives: you declare its
size, send it in parts, and complete it. What you get is an ordinary
[file](/docs/files#the-file-object).

| Route | What it does |
| --- | --- |
| \`POST /v1/uploads\` | Declare an upload: size, name, type, purpose. |
| \`POST /v1/uploads/{upload_id}/parts\` | Send a part as \`multipart/form-data\`, the way the SDKs do. |
| \`PUT /v1/uploads/{upload_id}/parts/{part_number}\` | Send a part as raw bytes, at a position you choose. |
| \`GET /v1/uploads/{upload_id}\` | See which parts have arrived — the route a resume starts from. |
| \`POST /v1/uploads/{upload_id}/complete\` | Finish: the file exists from this moment. |
| \`POST /v1/uploads/{upload_id}/cancel\` | Abandon the upload and discard its parts. |

Every upload route needs the \`files.write\` scope — reading an upload back
is part of writing it — and refuses \`Idempotency-Key\`; parts are idempotent
by their number instead.

## With the Python SDK

The \`openai\` Python package has a helper that does the whole thing, in
64 MiB parts:

~~~python
import os
from pathlib import Path
from openai import OpenAI

client = OpenAI(base_url="${API_BASE_URL}", api_key=os.environ["TECHSARA_API_KEY"], max_retries=5)

upload = client.uploads.upload_file_chunked(
    file=Path("standup.mp4"),
    mime_type="video/mp4",
    purpose="user_data",
)
print(upload.status, upload.file.id)          # completed file-… — at once, at any size

ready = client.files.wait_for_processing(upload.file.id, poll_interval=10, max_wait_seconds=6 * 3600)
print(ready.status, ready.id)
~~~

What the helper does not do: it sends parts one after another, it cannot
resume — a crash halfway starts again from the first byte, as a new upload —
and it sets no \`expires_after\`. Pass \`md5=\` with the file's MD5 to have
the assembled bytes checked. For a multi-gigabyte file on a connection you do
not trust, use the [resumable script](#a-resumable-upload) instead.

\`wait_for_processing\` gives up after 30 minutes unless told otherwise; a
long recording needs the larger \`max_wait_seconds\` shown.

## With the Node SDK

The \`openai\` Node package has no chunking helper, so the loop is yours. The
sample needs version 5 or later of the \`openai\` Node package, whose
\`client.put\` takes one type argument.
Read one part at a time — \`fs.readFileSync\` cannot read a file of 2 GiB or
more — and send each as a raw, numbered part:

~~~typescript
import fs from "node:fs";
import crypto from "node:crypto";
import OpenAI from "openai";

const PART = 64 * 1024 * 1024;
const client = new OpenAI({ baseURL: "${API_BASE_URL}", apiKey: process.env.TECHSARA_API_KEY, maxRetries: 5 });

const path = "standup.mp4";
const bytes = (await fs.promises.stat(path)).size;
const upload = await client.uploads.create({ bytes, filename: "standup.mp4", mime_type: "video/mp4", purpose: "user_data" });

const partIds: string[] = [];
const fh = await fs.promises.open(path, "r");
try {
  for (let n = 0, offset = 0; offset < bytes; n += 1, offset += PART) {
    const length = Math.min(PART, bytes - offset);
    const buf = Buffer.allocUnsafe(length);
    await fh.read(buf, 0, length, offset);
    const sha = crypto.createHash("sha256").update(buf).digest("hex");
    // A Buffer body is retried by the SDK; a numbered part is safe to send twice.
    const part = await client.put<{ id: string; part_number: number }>(\`/uploads/\${upload.id}/parts/\${n}\`, {
      body: buf,
      headers: { "Content-Type": "application/octet-stream", "X-Part-SHA256": sha },
    });
    partIds.push(part.id);
  }
} finally {
  await fh.close();
}

const done = await client.uploads.complete(upload.id, { part_ids: partIds });
const ready = await client.files.waitForProcessing(done.file!.id, { pollInterval: 10_000, maxWait: 6 * 3600 * 1000 });
console.log(ready.status);
~~~

Why \`client.put\` rather than \`client.uploads.parts.create\`: the typed
\`parts.create\` has no \`part_number\`, so its parts are *sequential* and
cannot be resumed out of order; and a part sent as a stream
(\`fs.createReadStream\`) is never retried by the SDK, so one dropped
connection fails the whole upload. A \`Buffer\` through \`client.put\` avoids
both.

## A resumable upload

The upload id is the only thing a resume needs. Save it before the first
part; after a crash, ask the API which parts it already has and send the rest.

~~~python
import hashlib
import json
import math
import os
from pathlib import Path
from openai import OpenAI

PART = 64 * 1024 * 1024
client = OpenAI(base_url="${API_BASE_URL}", api_key=os.environ["TECHSARA_API_KEY"], max_retries=5)

path = Path("standup.mp4")
size = path.stat().st_size
saved = Path("standup.mp4.upload.json")

def new_upload() -> str:
    upload_id = client.uploads.create(
        bytes=size, filename=path.name, mime_type="video/mp4", purpose="user_data",
    ).id
    saved.write_text(json.dumps({"upload_id": upload_id}))    # BEFORE the first part
    return upload_id

upload_id = json.loads(saved.read_text())["upload_id"] if saved.exists() else new_upload()
state = client.get(f"/uploads/{upload_id}", cast_to=dict)      # what already arrived
if state["status"] in ("expired", "cancelled"):                # its parts are gone: start over
    upload_id = new_upload()
    state = client.get(f"/uploads/{upload_id}", cast_to=dict)
if state["status"] == "completed":                             # the crash came after complete
    file_id = state["file"]["id"]
else:
    have = {p["part_number"] for p in state["parts"]}
    whole = hashlib.sha256()
    with path.open("rb") as fh:
        for n in range(math.ceil(size / PART)):
            chunk = fh.read(PART)
            whole.update(chunk)
            if n in have:
                continue
            client.put(
                f"/uploads/{upload_id}/parts/{n}",
                content=chunk,
                cast_to=dict,
                options={"headers": {
                    "Content-Type": "application/octet-stream",
                    "X-Part-SHA256": hashlib.sha256(chunk).hexdigest(),
                }},
            )
    done = client.post(f"/uploads/{upload_id}/complete", body={"sha256": whole.hexdigest()}, cast_to=dict)
    file_id = done["file"]["id"]

saved.unlink()
ready = client.files.wait_for_processing(file_id, poll_interval=10, max_wait_seconds=6 * 3600)
print(ready.status, ready.id)
~~~

\`client.get\`, \`client.put\` and \`client.post\` are the SDK's own request
methods: they carry the key, the retries and the timeouts of every typed call.
\`content=\` sends the part as raw bytes; it needs version 2.16.0 or later of
the \`openai\` Python package, and an older one fails with a \`TypeError\`.

A saved upload that has \`expired\` or been \`cancelled\` has lost its parts,
and every part sent to it is \`409\` for good, so the script starts a new
upload instead. One whose record is gone altogether — 30 days after it
ended — is \`404 upload_not_found\`: delete the saved file and run again.

The same with \`curl\`, \`jq\` and \`split\`. Run the first block once, then
the second as many times as it takes:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

FILE=standup.mp4
BYTES=$(stat -c %s "$FILE")

jq -n --arg name "$FILE" --argjson bytes "$BYTES" \\
  '{bytes: $bytes, filename: $name, mime_type: "video/mp4", purpose: "user_data"}' |
curl -s ${API_BASE_URL}/uploads \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  --data-binary @- | jq -r .id > upload.id      # keep this file: it is the resume

split -b 64M -d -a 5 "$FILE" part.
~~~

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

UPLOAD_ID=$(cat upload.id)
STATE=$(curl -s ${API_BASE_URL}/uploads/$UPLOAD_ID \\
  -H "Authorization: Bearer $TECHSARA_API_KEY")
case $(jq -r .status <<< "$STATE") in
  expired|cancelled) rm upload.id; echo "the upload lost its parts: run the first block again"; exit 1 ;;
  completed) jq '{status, file: .file.id}' <<< "$STATE"; exit 0 ;;
esac
HAVE=$(jq -r '.parts[].part_number' <<< "$STATE")

n=0
for part in part.*; do
  if ! grep -qx "$n" <<< "$HAVE"; then
    curl -s --fail-with-body -T "$part" ${API_BASE_URL}/uploads/$UPLOAD_ID/parts/$n \\
      -H "Authorization: Bearer $TECHSARA_API_KEY" \\
      -H "Content-Type: application/octet-stream" \\
      -H "X-Part-SHA256: $(sha256sum "$part" | cut -d' ' -f1)" || exit 1
  fi
  n=$((n + 1))
done

jq -n --arg sha "$(sha256sum standup.mp4 | cut -d' ' -f1)" '{sha256: $sha}' |
curl -s ${API_BASE_URL}/uploads/$UPLOAD_ID/complete \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  --data-binary @- | jq '{status, file: .file.id}'
~~~

\`curl -T\` sends the file with \`PUT\` and a \`Content-Length\`, which a raw
part requires.

## Create an upload

~~~http
POST /v1/uploads
~~~

~~~json
{
  "bytes": 2147483648,
  "filename": "standup.mp4",
  "mime_type": "video/mp4",
  "purpose": "user_data",
  "expires_after": { "anchor": "created_at", "seconds": 604800 }
}
~~~

| Field | Rule |
| --- | --- |
| \`bytes\` | Required: the exact size of the whole file, 0 to 107,374,182,400 (100 GiB). |
| \`filename\` | Required, up to 255 characters. |
| \`mime_type\` | Required, up to 255 characters. What the file *is* is read from its bytes; this type and the filename only break ties between text formats, as [kinds](/docs/files#kinds-and-what-processing-does) explains. |
| \`purpose\` | Required: \`user_data\` (or \`assistants\`, \`vision\`), as on [files](/docs/files#upload-a-small-file). |
| \`expires_after\` | Optional. It applies to the **file** the upload becomes, not to the upload. |

The body is JSON of at most 1 MiB, and any other field is a \`400\`. The
answer:

~~~json
{
  "id": "${EXAMPLE_UPLOAD_ID}",
  "object": "upload",
  "bytes": 2147483648,
  "created_at": 1789300000,
  "expires_at": 1789386400,
  "filename": "standup.mp4",
  "purpose": "user_data",
  "status": "pending",
  "file": null,
  "part_max_bytes": 67108864,
  "max_parts": 10000,
  "bytes_received": 0
}
~~~

\`part_max_bytes\` and \`max_parts\` are the ceilings in force; read them
rather than hard-coding them.

## Send the parts

Two routes, the same result. Either may be retried as often as you like.

**\`PUT /v1/uploads/{upload_id}/parts/{part_number}\`** takes the part as the
raw request body. \`Content-Length\` is required (a \`411\` without it), and
you may add the part's SHA-256, as \`X-Part-SHA256: <hex>\` or as
\`Content-Digest: sha-256=:<base64>:\`. This is the route for your own
clients and for resuming.

**\`POST /v1/uploads/{upload_id}/parts\`** takes \`multipart/form-data\` with
the bytes in a file field named \`data\` — what the SDKs send — plus two
optional text fields: \`part_number\` and \`sha256\` (hex).

~~~json
{
  "id": "${EXAMPLE_PART_ID}",
  "object": "upload.part",
  "created_at": 1789300100,
  "upload_id": "${EXAMPLE_UPLOAD_ID}",
  "part_number": 16,
  "bytes": 67108864,
  "sha256": "${EXAMPLE_SHA256}"
}
~~~

The rules:

* A part is **1 byte to 64 MiB**. Only the last part of a file is normally
  smaller than the rest, but nothing requires equal sizes.
* **Part numbers start at 0** and go to 9,999.
* **A part is whole or absent.** It is written to disk and checked before it
  counts; a connection that drops mid-part leaves nothing behind
  (\`408 incomplete_body\`), and a SHA-256 that does not match is
  \`400 checksum_mismatch\`.
* **Sending a numbered part again replaces it** and returns the same part id,
  so a retry whose answer was lost does no harm.
* **The first part decides the upload's numbering, for good.** A part sent
  with a number — any \`PUT\`, or a \`POST\` with \`part_number\` — makes the
  upload *numbered*; a \`POST\` without one makes it *sequential*, numbered
  in the order the parts finish arriving. Mixing the two is a \`400\`:
  sequential parts that finish out of order would otherwise be assembled out
  of order.
* A part that arrives after \`complete\` or \`cancel\` is
  \`409 upload_state_conflict\`, with \`x-should-retry: false\`.

There is no limit on how many parts are sent at once; up to 8 in parallel is
the range that has been tested.
${slowLinks(noTimeout)}

## Resume after a crash

\`GET /v1/uploads/{upload_id}\` returns the upload with every part that has
arrived:

~~~json
{
  "id": "${EXAMPLE_UPLOAD_ID}",
  "object": "upload",
  "bytes": 2147483648,
  "created_at": 1789300000,
  "expires_at": 1789386400,
  "filename": "standup.mp4",
  "purpose": "user_data",
  "status": "pending",
  "file": null,
  "part_max_bytes": 67108864,
  "max_parts": 10000,
  "bytes_received": 1140850688,
  "parts": [
    { "id": "${EXAMPLE_PART_ID}", "object": "upload.part", "created_at": 1789300010, "upload_id": "${EXAMPLE_UPLOAD_ID}", "part_number": 0, "bytes": 67108864, "sha256": "${EXAMPLE_SHA256}" }
  ],
  "part_mode": "numbered",
  "error": null
}
~~~

\`parts\` is in part-number order and complete — up to 10,000 entries, never
paginated. Send the numbers that are missing, then complete. If \`status\` is
already \`completed\`, the crash came after \`complete\`, and \`file\` holds
the file.

## Finish the upload

~~~http
POST /v1/uploads/{upload_id}/complete
~~~

~~~json
{ "part_ids": ["${EXAMPLE_PART_ID}"], "md5": "d41d8cd98f00b204e9800998ecf8427e", "sha256": "${EXAMPLE_SHA256}" }
~~~

| Field | Rule |
| --- | --- |
| \`part_ids\` | The parts, in the order they make up the file. Parts not listed are discarded. **Required for a sequential upload.** For a numbered upload it may be left out: the parts are then joined in number order and must run from 0 without a gap — otherwise the \`400\` lists the missing numbers. |
| \`md5\` | Optional: the MD5 of the whole file, in hex. |
| \`sha256\` | Optional: the SHA-256 of the whole file, in hex. |

The parts listed must add up to exactly the \`bytes\` the upload declared,
or the answer is a \`400\` saying how many bytes they hold, and the upload
stays open for you to fix it.

**\`complete\` answers \`200\` at once, at any size**, with the upload
\`completed\` and the new file nested in it:

~~~json
{
  "id": "${EXAMPLE_UPLOAD_ID}",
  "object": "upload",
  "bytes": 2147483648,
  "created_at": 1789300000,
  "expires_at": 1789386400,
  "filename": "standup.mp4",
  "purpose": "user_data",
  "status": "completed",
  "part_max_bytes": 67108864,
  "max_parts": 10000,
  "bytes_received": 2147483648,
  "file": {
    "id": "${EXAMPLE_FILE_ID}",
    "object": "file",
    "bytes": 2147483648,
    "created_at": 1789303600,
    "filename": "standup.mp4",
    "purpose": "user_data",
    "status": "uploaded",
    "status_details": null,
    "expires_at": null,
    "sha256": null,
    "mime_type": null,
    "processing": { "state": "processing", "kind": "unknown", "stage": "assemble", "step": 0, "percent": 0 }
  }
}
~~~

The parts are joined into one file *after* this answer, as the file's first
processing stage, \`assemble\`, with a \`percent\` you can watch like any
other stage. A 100 GiB upload takes several minutes to assemble; nothing waits
for it. The file id is usable straight away — for
\`wait_for_processing\`, for [events](/docs/files#progress-events), and in a
model request, which waits for the file to be ready.

The checksums are verified during assembly, because they need every byte. If
one does not match, the **file** ends \`status: "error"\` with
\`checksum_mismatch\` and its parts are discarded; the upload itself stays
\`completed\`. Upload the file again.

\`complete\` can be repeated: once completed, it returns the same body, with
the file's current state. A second \`complete\` that arrives while the first
is still being recorded is \`409 upload_state_conflict\` with
\`Retry-After: 2\` and \`x-should-retry: true\`; its retry gets the first
one's answer.

## Cancel an upload

\`POST /v1/uploads/{upload_id}/cancel\`, with no body, discards every part and
returns the upload with \`status: "cancelled"\`. Cancelling again returns the
same. A completed upload cannot be cancelled (\`409\`) — delete its
[file](/docs/files#delete-a-file) instead — and one being completed at that
moment answers \`409\` with \`Retry-After: 2\`.

## States and expiry

| \`status\` | Meaning |
| --- | --- |
| \`pending\` | Accepting parts. |
| \`finalizing\` | A \`complete\` is being recorded — a moment, not a phase. |
| \`completed\` | Done; \`file\` is the file. |
| \`cancelled\` | Cancelled; the parts are gone. |
| \`expired\` | Nothing arrived for too long; the parts are gone. |

A pending upload does not expire on a fixed clock from creation. Its
\`expires_at\` is **24 hours after the last part that arrived**, and never
more than **7 days** after the upload was created — so a very large upload
over a slow connection is not cut off while it is still making progress.
Finished, cancelled and expired uploads can still be read back for 30 days.

## Limits and why

Technical ceilings, not usage limits: there is no cap on how many uploads a
project makes.

| Ceiling | Value | Why |
| --- | --- | --- |
| One part | 1 byte to 67,108,864 bytes (64 MiB) | The network edge in front of the API accepts at most 100,000,000 bytes per request; 64 MiB leaves room for framing, and it is the part size the Python SDK uses by default. |
| Request body of a \`POST\` part | 68,157,440 bytes | The part plus 1 MiB of multipart framing. |
| Parts per upload | 10,000 (numbers 0 to 9,999) | A \`complete\` naming 10,000 part ids is about 310 KB of JSON, inside the 1 MiB JSON body. |
| One upload | 107,374,182,400 bytes (100 GiB) | Assembly briefly needs room for the parts and the joined file together, on storage the rest of the service shares. |
| JSON body of create and complete | 1 MiB | The rule for every JSON route. |
| A pending upload | 24 hours after its last part, 7 days at most | Parts of an abandoned upload do not hold storage forever. |

A file made from an upload then meets the per-kind processing ceilings on
[files](/docs/files#limits-and-why) — a 90 GiB video is accepted as an upload,
and processed if it is no longer than 4 hours.

## Errors

| Code | Status | When | Retry? |
| --- | --- | --- | --- |
| \`upload_not_found\` | 404 | The upload id is absent, malformed or another project's. | No |
| \`upload_state_conflict\` | 409 | A part, \`complete\` or \`cancel\` for an upload in the wrong state, with \`x-should-retry: false\`. The one exception is a \`complete\` that meets another \`complete\` in progress: \`Retry-After: 2\` and \`x-should-retry: true\`. | Only when \`x-should-retry\` is \`true\` |
| \`checksum_mismatch\` | 400 | A part's SHA-256 does not match its bytes (\`param\` \`sha256\`). | No — send the right bytes |
| \`incomplete_body\` | 408 | The connection closed mid-part. Nothing was recorded. | Yes |
| \`request_too_large\` | 413 | A part over 64 MiB. | No |
| \`storage_unavailable\` | 503 | The service is short of storage (\`Retry-After: 60\`, \`x-should-retry: false\`). | Later |
| \`invalid_request_error\` | 400 | A bad field; a part of 0 bytes; mixed numbering; a part over the upload's budget; a \`complete\` whose parts do not add up, are missing, or are not this upload's. | No |
| \`invalid_request_error\` | 411 | A raw \`PUT\` part without \`Content-Length\`. | No |

Both SDKs retry \`408\`, \`409\` and \`5xx\` answers by default, and both obey
\`x-should-retry\` first — which is why a state conflict that no retry can
fix says \`false\`.
`.trim(),
  };
}

export const uploads: DocPage = uploadsPage({ noTimeout: NO_TIMEOUT_LIVE });
