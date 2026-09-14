import type { DocPage } from '../types';
import {
  API_BASE_URL,
  DEPLOYS_HELD,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_STATUS,
  MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';
import { FILES_API_PUBLISHED } from './files';

// 2026-09-13, no-timeout design (revision 2, sdk_and_docs): the page a
// developer reads before sending anything long. Every setting on it was
// measured against the SDKs, not assumed: openai-python treats its timeout as
// a limit on silence and `0` as "fail now"; openai-node 7.5+ turns its timeout
// into a deadline for the whole non-streamed call and aborts at once on `0`,
// `Infinity` or anything at or above 2^31 ms. A snippet that got either of
// those wrong would cut the exact requests this page exists to protect.
//
// Listed on the site only once the no-timeout release is live
// (content/docs/index.ts, NO_TIMEOUT_LIVE in pages/longOutput.ts); until then
// the rest of the site describes the API as it runs today.
//
// 2026-09-14, review of this page: two more things it must never get wrong.
// (1) The resume loop stops on every terminal event of CONTRACT §10.2 —
// `error` included — and calls the reader's own handler outside the except
// clauses that swallow a dropped connection: a loop that only knew
// completed/failed spun for ten minutes on a run that had ended with `error`
// and then raised a misleading TimeoutError, and one that caught the handler's
// exceptions skipped an event for good. (2) "Deploys are invisible to a
// connected client" is true only once /v1 reaches the gateway through the
// public URL, which is an operator step after the deploy; until a run through
// that URL is recorded (DEPLOYS_HELD in samples.ts) the page says a deploy can
// close the connection and the loop below picks the generation up.

/**
 * The page, with or without the Files API sentences (FILES_API_PUBLISHED),
 * and with the deploy promise only once it was measured (DEPLOYS_HELD).
 */
export function timeoutsPage({
  filesPublished,
  deploysHeld = DEPLOYS_HELD,
}: {
  filesPublished: boolean;
  deploysHeld?: boolean;
}): DocPage {
  const deploys = deploysHeld
    ? `Deploys on
our side are invisible to a connected client: the connection is held while the
service restarts, and the generation carries on from where it stopped. What no
server can hide is the network between you and us dropping the connection —
run the [resume loop](#resuming-a-stream) and that is survivable too.`
    : `A deploy on
our side can close a connected client's connection, but not the generation
behind it: that carries on from where it stopped. [Resume](#resuming-a-stream)
a stream, or retry a synchronous call with the same \`Idempotency-Key\`, and
you pick it up — the same way you survive the network between you and us
dropping the connection.`;
  const unreachable = deploysHeld
    ? `* **Our service stays unreachable for 30 minutes** while your connection is
  held open waiting for it.`
    : `* **Our service stays unreachable** for longer than your client keeps
  retrying. The connection is closed, and the generation is then treated as
  abandoned, as below.`;
  const largerAudio = filesPublished
    ? `Larger recordings go through [uploads](/docs/uploads): send the file in
parts with \`client.uploads.upload_file_chunked\`, then transcribe it by its
\`file_id\`.`
    : `A recording larger than that has to be cut into files of at most 89 MiB
each.`;

  return {
    slug: 'timeouts',
    title: 'Long requests, timeouts and resuming',
    summary:
      'There are no server timeouts on /v1. The client settings that let a ' +
      'request run for hours, and how to resume one the network cut.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: `
**There are no server timeouts.** No request on \`/v1\` is ended because it
has been running for a long time: not a synchronous call, not a stream, not a
background job, not a queue wait. A request ends when its work is done, when
you cancel it, or in one of the few cases under
[what still ends a request](#what-still-ends-a-request).

What makes that safe through every proxy between you and the API is one rule.

## The 15-second rule

After your request is authenticated and validated, **its first byte is sent
within 15 seconds, and another byte at least every 15 seconds after that**,
for as long as it runs.

| Mode | What keeps the connection alive |
| --- | --- |
| \`"stream": true\` | \`200\` and the first event at once. A \`: ping\` comment whenever 15 seconds pass without an event. |
| Synchronous JSON | The real status and body, if they are ready within 15 seconds. Otherwise \`200\` at 15 seconds, then a space every 15 seconds, then the JSON object. |
| \`"background": true\` | \`202\` at once. The answer is collected later. |

No proxy on the way sees a silent connection, so none closes one — including
the one in front of the API, which gives up on a response whose first byte takes
more than 125 seconds. ${deploys}

## Python

With the \`openai\` package, change the client, not every call:

~~~python
import os
import uuid
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)

answer = client.responses.create(
    model="${MODEL_ID}",
    input="Write the complete field manual, chapter by chapter.",
    max_output_tokens=200000,
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},
)
~~~

* **\`None\`, never \`0\`.** \`Timeout(None, connect=10.0)\` turns the read
  timeout off and keeps a 10-second limit on opening the connection. \`0\` does
  not mean "no limit" to this client: it fails every call at once.
* The default, a 600-second limit on silence, already works — the API's bytes
  reset it — but \`None\` also covers a proxy of your own that buffers.
* **One \`Idempotency-Key\` per call**, passed with \`extra_headers\`, on
  \`/v1/responses\` and \`/v1/chat/completions\`. A retry with the same key
  attaches to the generation already running instead of starting another.
  Do not set it as a client-wide default header: the embeddings, rerank and
  transcription endpoints refuse the header.
* An SDK retry of an identical request, from the same key, also attaches
  without an \`Idempotency-Key\` — the client marks its retries, and the API
  matches the body. The key is what makes it a guarantee.

## Node

With the \`openai\` package:

~~~typescript
import OpenAI from "openai";
import { randomUUID } from "node:crypto";

const client = new OpenAI({
  baseURL: "${API_BASE_URL}",
  apiKey: process.env.TECHSARA_API_KEY,
  timeout: 2_147_483_647,
  maxRetries: 5,
});

const answer = await client.responses.create(
  {
    model: "${MODEL_ID}",
    input: "Write the complete field manual, chapter by chapter.",
    max_output_tokens: 200000,
  },
  { headers: { "Idempotency-Key": randomUUID() } },
);
~~~

* **2,147,483,647 milliseconds is the largest timeout this client honours.**
  \`0\`, \`Infinity\` and anything larger abort every call at once.
* From version 7.5 the timeout covers the **whole** non-streamed call, not just
  the silence in it. With the default 600,000, a synchronous call still
  running after 10 minutes is abandoned by the SDK — each of its retries joins
  the same generation, but the SDK gives up after its third 10-minute attempt.
  For work that may take longer, use \`stream: true\` or \`background: true\`,
  or keep the timeout at the maximum above.
* Node's own \`fetch\` has 300-second header and body timers. The API's bytes
  reset them, so leave them alone — unless you run a proxy of your own that
  goes silent, in which case give the client a dispatcher without them:

~~~typescript
import OpenAI from "openai";
import { Agent, fetch } from "undici";

const client = new OpenAI({
  baseURL: "${API_BASE_URL}",
  apiKey: process.env.TECHSARA_API_KEY,
  timeout: 2_147_483_647,
  maxRetries: 5,
  fetch,
  fetchOptions: { dispatcher: new Agent({ headersTimeout: 0, bodyTimeout: 0 }) },
});
~~~

## curl

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -N ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -d '{"model": "${MODEL_ID}", "input": "Write the complete field manual.", "max_output_tokens": 200000, "stream": true}'
~~~

\`-N\` prints events as they arrive. Do not add \`--max-time\`: it is a limit
on the whole transfer, and a long answer will reach it.

## Long synchronous calls

A synchronous call can run for hours. Three things are different about one
that runs past 15 seconds:

* **The \`200\` arrives before the answer exists.** It is followed by spaces,
  then the JSON object. JSON parsers, including both SDKs', skip the spaces.
* **A failure can still happen after that \`200\`.** It arrives in the body: a
  response with \`"status": "failed"\` and its \`error\` on
  [\`/v1/responses\`](/docs/responses), or \`"choices": []\` with an
  \`error\` on [\`/v1/chat/completions\`](/docs/chat-completions). Always
  check \`status\`, or \`choices\`, before you use the answer.
* **A transcription asked for as \`response_format=text\` starts with those
  spaces.** The SDK hands you the body as a string: call \`.strip()\` on it.
  For long audio prefer \`json\`, or \`stream=true\`.

An SDK's own retries back off for a few seconds in total, which is shorter
than a restart of the service can take when the connection cannot be held.
Wrap a call that must not be lost in a loop of your own, for at least ten
minutes, reusing the same key:

~~~python
import random
import time
import uuid
from openai import APIConnectionError, APIStatusError, APITimeoutError

RETRY_STATUSES = {502, 503, 524, 530}

def create_patiently(client, **params):
    key = str(uuid.uuid4())                  # the same key on every attempt
    delay, give_up_at = 1.0, time.monotonic() + 600
    while True:
        try:
            response = client.responses.create(**params, extra_headers={"Idempotency-Key": key})
            if response.status == "failed":
                raise RuntimeError(f"{response.error.code}: {response.error.message}")
            return response
        except (APIConnectionError, APITimeoutError):
            pass
        except APIStatusError as exc:
            if exc.status_code not in RETRY_STATUSES:
                raise
        if time.monotonic() > give_up_at:
            raise TimeoutError("still unreachable after ten minutes")
        time.sleep(delay + random.uniform(0, 1))
        delay = min(60.0, delay * 2)
~~~

## Resuming a stream

Keep two things from a Responses stream: the response id, from
\`response.created\`, and the \`sequence_number\` of the last event you
handled. Every event carries one, starting at 1 and going up by one. When the
stream raises, or ends without one of its three terminal events —
\`response.completed\`, \`response.failed\` or \`error\` — ask for the rest:

~~~http
GET /v1/responses/{id}?stream=true&starting_after=412
~~~

~~~python
import time
from openai import APIStatusError

def events_after(client, response_id, last_seq):
    """The events after last_seq. Ends quietly when the connection drops."""
    try:
        with client.responses.retrieve(response_id, stream=True, starting_after=last_seq) as stream:
            yield from stream
    except APIStatusError as exc:
        if exc.status_code < 500:
            raise                    # 400 or 404: resuming again will not change it
    except Exception:
        pass                         # a dropped connection: the HTTP library's own error, mid-stream

def resume(client, response_id, last_seq, handle):
    """Follow a response from after last_seq to its terminal event."""
    delay, give_up_at = 1.0, time.monotonic() + 600
    while True:
        for event in events_after(client, response_id, last_seq):
            handle(event)            # your code: an exception from it is raised, never retried
            last_seq = event.sequence_number                    # only once it was handled
            delay, give_up_at = 1.0, time.monotonic() + 600     # progress resets the budget
            if event.type == "error":
                raise RuntimeError(f"{response_id}: {event.code}: {event.message}")
            if event.type in ("response.completed", "response.failed"):
                return event.response
        if time.monotonic() > give_up_at:
            raise TimeoutError(f"no progress for ten minutes on {response_id}")
        time.sleep(delay)
        delay = min(60.0, delay * 2)
~~~

~~~typescript
import OpenAI, { APIConnectionError, APIError } from "openai";

async function* eventsAfter(client: OpenAI, responseId: string, lastSeq: number) {
  try {
    const stream = await client.responses.retrieve(responseId, { stream: true, starting_after: lastSeq });
    for await (const event of stream) yield event;
  } catch (err) {
    if (err instanceof APIConnectionError) return;                    // could not connect: resume
    if (err instanceof APIError && (err.status ?? 0) < 500) throw err;  // 400, 404, or the stream's error event
    // anything else is the connection dropping mid-stream ("TypeError: terminated"): resume
  }
}

async function resume(client: OpenAI, responseId: string, lastSeq: number,
                      handle: (event: OpenAI.Responses.ResponseStreamEvent) => unknown) {
  let delay = 1_000;
  let giveUpAt = Date.now() + 600_000;
  for (;;) {
    for await (const event of eventsAfter(client, responseId, lastSeq)) {
      await handle(event);        // your code: an exception from it is thrown, never retried
      lastSeq = event.sequence_number;
      delay = 1_000;
      giveUpAt = Date.now() + 600_000;
      if (event.type === "response.completed" || event.type === "response.failed") return event.response;
    }
    if (Date.now() > giveUpAt) throw new Error(\`no progress for ten minutes on \${responseId}\`);
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(60_000, delay * 2);
  }
}
~~~

~~~bash
curl -N "${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}?stream=true&starting_after=412" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

The API replays every event after \`starting_after\`, follows the generation
live, and closes after the terminal event — at once, for a response that has
already finished.

Three things in those loops are deliberate:

* **Any exception from the stream, except a \`4xx\`, means "resume".** A
  connection that drops in the middle of a stream does not raise one of the
  SDK's own errors: the HTTP library underneath raises — in Python a
  \`RemoteProtocolError\` ("incomplete chunked read"), in Node a
  \`TypeError: terminated\`.
* **\`error\` is a terminal event, like \`response.failed\`.** The run is over;
  resuming it again replays nothing new. The Python SDK hands it to you as an
  event, and the Node SDK throws it as an \`APIError\` with no HTTP status —
  both loops stop on it.
* **Your handler runs outside the \`except\`.** An exception from your own code
  is raised at once instead of being mistaken for a dropped connection, and
  \`last_seq\` moves past an event only after you handled it.

* \`stream=true\` is required; \`starting_after\` without it is a \`400\`.
* **Only the key that created the response can replay it**, or another key of
  the same service account. Any other key gets \`404\`, as if the response did
  not exist — even one with \`responses.read\` in the same project.
* Events are kept while the generation runs and for **one hour** after it
  ends. After that, or for a response created with \`"store": false\`, the
  replay is a \`400\` on \`stream\`.
* Back off from 1 second, doubling to 60, for at least ten minutes before
  giving up.

**Chat Completions** has no response id to resume from. Retry the same request
with the same \`Idempotency-Key\`: the stream replays its chunks from the start
and then continues live to \`[DONE]\`, with the same completion id. Discard
what you had, or skip as many characters as you already have.

## Waiting for capacity

Busy never means \`429\`, and never \`503\`. A request that has to wait for an
engine waits, however long that takes, and says so:

* a Responses stream sends \`response.queued\`, then \`: ping\` comments;
* a Chat Completions stream sends \`: queued\` comments;
* a synchronous call sends spaces;
* a background response stays \`queued\`.

A \`503\` now means only that an engine is down or restarting, or that a
physical safeguard of the service tripped — too many open connections, or too
little free disk — and it carries a \`Retry-After\` of 60 seconds or less. A
chat turn in the TechSara application that needs the long-context lane can
pause a very long job of yours for a moment; it resumes by itself, and the text
is unchanged.

## Audio and files

A transcription has no duration limit. One request carries up to **90 MiB** —
an 89 MiB file and its form fields. ${largerAudio}

* Above about 10 minutes of audio, send \`stream=true\`: the transcript
  arrives as it is written, with \`transcript.text.delta\` events.
* A retry of the same file does not start again from the beginning: finished
  parts of the work are reused.

~~~python
with open("board-meeting.m4a", "rb") as audio:
    transcript = client.audio.transcriptions.create(
        model="${WHISPER_MODEL_ID}",
        file=("board-meeting.m4a", audio, "audio/mp4"),
        response_format="text",
    )
print(transcript.strip())
~~~

## What still ends a request

* The engine is **proven down for 30 minutes** without a break. Shorter
  outages, restarts and recoveries are waited out.
* **Three attempts in a row make no progress** while the engine is otherwise
  serving.
* **The same request is caught up in two engine crashes.** It fails with
  \`x-should-retry: false\`: a request that twice coincided with a crash is
  not sent a third time automatically.
* **The key is revoked** while the request runs.
${unreachable}
* **You disconnect and do not come back** within **10 minutes** — for a
  request with an \`Idempotency-Key\`, or a Responses stream — or within
  **2 minutes** for anything else. Until then a retry or a resume finds the
  request still running. A background response is never cancelled this way.
* **A \`"store": false\` request meets a restart** of the service. Such a
  request is also cancelled the moment its client disconnects, and cannot be
  resumed.

## What is stored

To make resuming possible, a running request's prompt is kept while it runs,
and the events it streams are kept for **one hour** after it ends. They are
readable only by the key that created the request, or a key of the same
service account. Send \`"store": false\` to keep nothing, at the price listed
above.
`.trim(),
  };
}

export const timeouts: DocPage = timeoutsPage({ filesPublished: FILES_API_PUBLISHED });
