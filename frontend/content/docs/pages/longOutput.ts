import type { DocPage } from '../types';
import {
  API_BASE_URL,
  DEPLOYS_HELD,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_STATUS,
  LONG_OUTPUT_WALL_CLOCK_LIVE,
  MODEL_ID,
  WALL_CLOCK_PENDING_NOTE,
} from '../samples';

// 2026-09-13, owner decision: max_output_tokens on techsara-35b goes to
// 1,000,000 — its whole context window — with input and output sharing that
// window, an over-long request clamped rather than refused, and the applied
// value reported back. This page exists because every one of those three
// facts changes client code, and because a 1M-token answer takes hours, which
// changes which request mode is safe to use at all (CONTRACT §8.3).

/**
 * WHETHER THE NO-TIMEOUT RELEASE IS LIVE — this page's second switch.
 *
 * The no-timeout design (2026-09-13) removes the per-generation wall clock
 * from /v1, sends a first byte within 15 s and a byte every 15 s after it,
 * keeps generations running across deploys, and lets a broken stream resume
 * with `GET /v1/responses/{id}?stream=true&starting_after=N`. None of that is
 * on the running API yet, and the sections that describe it would be false
 * today. So the page renders the CURRENT sections while this is false — byte
 * for byte the page tests/docs-site.test.tsx checks — and the no-timeout
 * sections once it is true.
 *
 * tests/docs-files.test.tsx ties the value to the code: the day
 * publicapi/keepalive.py exists and the modules that serve /v1 generations
 * (publicapi/router.py, streaming.py, endpoints.py or main.py) import it and
 * serve `starting_after` — as code, not in a comment or a docstring — it
 * fails until this is flipped — and the wall-clock tests
 * in docs-site.test.tsx are rewritten in the same change. The Files pages
 * read it too, for the few sentences that change with it.
 */
export const NO_TIMEOUT_LIVE: boolean = true;

// ---------------------------------------------------------------- shared --
// The introduction as it reads today, and the sections true in both worlds.

const INTRO_CURRENT = `
\`${MODEL_ID}\` can generate up to **1,000,000 tokens** in one response — its
entire context window. That is a real ceiling, and it comes with three facts
your client has to handle: input and output share the window, a long answer
takes hours, and a synchronous request cannot wait that long.
${LONG_OUTPUT_WALL_CLOCK_LIVE ? '' : `\n${WALL_CLOCK_PENDING_NOTE}\n`}`;

const CEILING = `
## The ceiling

| Model | Largest \`max_output_tokens\` | When you do not ask |
| --- | --- | --- |
| \`${MODEL_ID}\` | 1,000,000 | 8,192 |
| \`techsara-8b-vision\` | 24,576 | 8,192 |
| \`techsara-ocr\` | 8,192 | 8,192 |

The default did not change: a request that does not send
\`max_output_tokens\` gets 8,192, as before. A value above the model's ceiling
— or above a lower ceiling your project sets — is a \`400\` with \`param\`
\`max_output_tokens\`. Read the ceilings from
[\`GET /v1/models\`](/docs/models) rather than from this table.
`;

const SHARED_WINDOW = `
## Input and output share one window

The context window holds your prompt *and* the answer. A request for more
output than the prompt leaves room for is **clamped, not refused**:

~~~text
applied max_output_tokens = min(requested, context_window − input_tokens − reserve)
~~~

The reserve is a few hundred tokens of safety margin (512 on \`${MODEL_ID}\`).
With a 300,000-token prompt and \`"max_output_tokens": 1000000\`, the applied
ceiling is about 699,488. The request runs; it is simply not allowed to run
past the end of the window, which it could never have done anyway.

The one size refusal left is the **prompt** itself: over the model's
\`max_input_tokens\` is \`400 context_length_exceeded\`, before anything runs.

**The window is reached through output, not input.** All the text in one
request body is limited to 1 MiB, which is roughly 250,000 to 350,000 tokens
of prose. A prompt cannot fill a million-token window; an answer can.
`;

const RESPONSE_FIELDS = `
## What the response tells you

Every response object carries two fields for this:

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789200000,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    { "type": "message", "role": "assistant", "content": [{ "type": "output_text", "text": "…" }] }
  ],
  "max_output_tokens": 699488,
  "incomplete_details": { "reason": "max_output_tokens" },
  "usage": { "input_tokens": 300000, "output_tokens": 699488, "total_tokens": 999488 }
}
~~~

| Field | Meaning |
| --- | --- |
| \`max_output_tokens\` | The ceiling **applied** to this generation, after clamping. Events before generation starts carry the planned value; the final event and \`GET /v1/responses/{id}\` carry the exact one. |
| \`incomplete_details\` | \`{"reason": "max_output_tokens"}\` when the answer stopped because it reached that ceiling; \`null\` when the model finished on its own. |

\`status\` stays \`completed\` when the ceiling stopped the answer. If you know
other APIs of this shape, note the difference: there is no \`incomplete\`
status here, so check \`incomplete_details\`, not \`status\`, to learn whether
the text was cut.

On [Chat Completions](/docs/chat-completions) the same facts are
\`finish_reason: "length"\` and a top-level \`max_output_tokens\` on the
\`chat.completion\` object and on the streamed chunk that carries
\`finish_reason\`.
`;

const HOW_LONG = `
## How long it takes

Output is written one token at a time. Measured decode speed on
\`${MODEL_ID}\` is roughly 70 to 100 tokens per second, depending on how long the
context already is and how busy the engine is:

| Output tokens | At 100 tokens/s | At 70 tokens/s |
| --- | --- | --- |
| 5,000 | 50 seconds | 71 seconds |
| 8,192 | 82 seconds | 2 minutes |
| 100,000 | 17 minutes | 24 minutes |
| 1,000,000 | 2 hours 47 minutes | 3 hours 58 minutes |

A very long prompt adds time before the first token too. Plan a million-token
answer as a job that runs for an afternoon, not as a request.
`;

// --------------------------------------------------------------- current --
// The API as it runs today: a wall clock per generation, and a proxy that
// gives up on a silent synchronous response.

const WALL_CLOCK = `
## The wall clock

Every generation has a time limit, sized to what you asked for so a large
request is not cut off early:

~~~text
wall clock (seconds) = min(21600, max(4200, 900 + planned_max_output_tokens / 50))
~~~

900 seconds covers reading a very long prompt, and 50 tokens per second is a
deliberately pessimistic decode speed. On \`techsara-8b-vision\` and
\`techsara-ocr\` the same rule uses 600, 60 and 20.

| \`max_output_tokens\` applied | Wall clock |
| --- | --- |
| 8,192 (the default) | 4,200 s — 70 minutes |
| 200,000 | 4,900 s — 1 hour 22 minutes |
| 500,000 | 10,900 s — 3 hours 2 minutes |
| 1,000,000 | 20,900 s — 5 hours 48 minutes |

No generation runs longer than 21,600 seconds (6 hours). A generation that
reaches its wall clock ends \`failed\` with the code \`timeout\` — and **keeps
what it wrote**: on a stream the \`response.failed\` event carries the partial
text and the usage; a background response keeps its partial \`output\`; a
synchronous request gets a \`504\`. You are charged for the tokens that were
generated, as always. The engine sends its token counts only at the very end
of a stream, so for a response stopped by its wall clock the server counts
them itself: the output tokens it received, and your prompt as counted before
the call.
`;

const CHOOSE_MODE = `
## Choose the mode before you choose the size

**A synchronous request is the wrong tool for a long answer.** It sends
nothing until the answer is complete, and the path to this API does not wait
forever for a first byte: the public hostname sits behind a proxy that gives
up on a silent response after **100 seconds** (you see an HTTP \`524\`), and
the edge in front of the API allows 300 seconds. At 70 to 100 tokens per
second, 100 seconds is about **5,000 tokens** — so even the 8,192 default can
be cut off in a synchronous call. If your client (or that proxy) closes the
connection, the server stops the generation there and records the response as
\`cancelled\` — it does not keep generating an answer nobody will receive.

| Expected output | Use |
| --- | --- |
| Under about 5,000 tokens | Any mode. |
| Above about 5,000 tokens | \`"stream": true\` if something is watching; \`"background": true\` if not. |
| Tens of thousands of tokens or more | \`"background": true\`, collected by [polling or webhook](/docs/background). |

Whatever the mode, send an [\`Idempotency-Key\`](/docs/idempotency). A retry
of a multi-hour request without one starts a second multi-hour request.
`;

const LONG_STREAM = `
## A long stream

A stream sends its headers immediately and a heartbeat comment at least every
15 seconds for as long as it runs — hours, if need be — so no proxy on the way
sees an idle connection. What can still break it is your own client's timeout:
turn the read timeout off for the stream, and write the text somewhere as it
arrives instead of holding it all in memory.

~~~python
import json
import os
import uuid
import httpx

payload = {
    "model": "${MODEL_ID}",
    "input": "Write the complete field manual, chapter by chapter.",
    "max_output_tokens": 1000000,
    "stream": True,
}
headers = {
    "Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}",
    "Idempotency-Key": str(uuid.uuid4()),
}

with httpx.stream("POST", "${API_BASE_URL}/responses", headers=headers,
                  json=payload, timeout=httpx.Timeout(30.0, read=None)) as response:
    response.raise_for_status()
    event = None
    with open("manual.txt", "w", encoding="utf-8") as out:
        for line in response.iter_lines():
            if not line:
                event = None
                continue
            if line.startswith(":"):                 # heartbeat, every 15 s at most
                continue
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
                if event == "response.output_text.delta":
                    out.write(data["delta"])
                elif event in ("response.completed", "response.failed"):
                    final = data["response"]
                    print("status:", final["status"],
                          "applied:", final["max_output_tokens"],
                          "cut:", final["incomplete_details"])
~~~

A dropped stream cannot be resumed, and the text of a streamed response is not
stored. For anything you cannot afford to lose halfway, use background.
`;

const LONG_BACKGROUND = `
## A long background job

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: aaaa-bbbb-cccc-dddd" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Write the complete field manual, chapter by chapter.",
    "max_output_tokens": 1000000,
    "background": true
  }'
~~~

The \`202\` carries the planned \`max_output_tokens\`. Then poll
\`GET /v1/responses/{id}\` slowly — once a minute is plenty for a job measured
in hours — or let a [webhook](/docs/webhooks) tell you it finished. The stored
response keeps the whole text for your project's retention window.

Two things to plan for:

* **It may wait before it starts.** A very long generation takes one of a
  small number of places reserved for long public work on the main engine,
  and waits for one while the row stays \`queued\` — for up to an hour — before
  it ends \`failed\` with \`model_unavailable\`. That is the engine's capacity,
  shared by every caller, not a limit on your project.
* **A service restart ends it.** A response that is running when the service
  restarts — which happens when a new version is deployed — ends \`failed\`
  with \`model_unavailable\` and the message "The service restarted while this
  response was running." It is safe to submit again — with a new
  \`Idempotency-Key\`, because the old one still names the failed job. The
  longer the job, the likelier it is to meet a restart, so treat a failure
  after hours as something your client retries, not as a bug.
`;

// ------------------------------------------------------------ no timeout --
// The API once the no-timeout release is integrated (NO_TIMEOUT_LIVE).

const INTRO_NO_TIMEOUT = `
\`${MODEL_ID}\` can generate up to **1,000,000 tokens** in one response — its
entire context window. That is a real ceiling, and it comes with three facts
your client has to handle: input and output share the window, a long answer
takes hours, and your own client must be willing to wait that long. The API
will: nothing on the server gives up on a generation because of the clock.
`;

const noWallClock = (deploysHeld: boolean): string => `
## The wall clock

**There is none.** A generation on \`/v1\` has no time limit. It ends when the model finishes,
when it reaches its applied \`max_output_tokens\`, when you cancel it, or in
one of the few cases listed under
[what still ends a generation](#what-still-ends-a-generation) — never
because it has been running for a long time.

What makes that work across every proxy between you and the API is one rule:
**after your request is authenticated and validated, its first byte is sent
within 15 seconds, and another byte at least every 15 seconds after that**,
for as long as it runs.

| Mode | How the connection is kept alive |
| --- | --- |
| \`"stream": true\` | \`200\` and the first event at once; a \`: ping\` comment whenever 15 seconds pass without an event. |
| Synchronous | The real answer if it is ready within 15 seconds. Otherwise \`200\` is sent at 15 seconds, then a space every 15 seconds, then the JSON object. |
| \`"background": true\` | Nothing to keep alive: \`202\` at once, and the answer is collected later. |

${deploysHeld
  ? `Routine deploys on our side do not break a connected client: the connection
is held while the service restarts, and the generation continues from where it
stopped. What no server can hide is the network between you and us dropping
the connection — a client that [resumes](#resuming-a-stream) survives that
too.`
  : `A routine deploy on our side can close a connected client's connection, but
not the generation: it continues from where it stopped, and a client that
[resumes](#resuming-a-stream) — or retries a synchronous call with the same
\`Idempotency-Key\` — picks it up. The network between you and us dropping
the connection is survived the same way.`}

**A synchronous answer can start before it is known.** When the \`200\` has
already been sent and the generation then fails, the failure is in the body:
a response with \`"status": "failed"\` and its \`error\` on
[\`/v1/responses\`](/docs/responses), or \`"choices": []\` with an \`error\`
on [\`/v1/chat/completions\`](/docs/chat-completions). Always check
\`status\` — a \`200\` alone does not mean the answer is complete. JSON
parsers, including both SDKs', ignore the leading spaces.

Sometimes a failure cannot be written into the body that way, and the
connection is closed instead: the body is spaces, or part of an object, and
then ends. Both SDKs treat that as a dropped connection and retry. Your own
client should do the same with a body that holds no complete JSON object —
retry it with the same [\`Idempotency-Key\`](/docs/idempotency), so the retry
joins the generation instead of starting another.

**Waiting for capacity is never an error.** A busy engine shows as
\`response.queued\` on a Responses stream, as \`: queued\` comments on a Chat
Completions stream, and as more spaces on a synchronous call. A \`503\` now
means an engine is down or restarting, or a physical safeguard of the service
tripped, and carries a \`Retry-After\` of 60 seconds or less.
`;

const EVERY_MODE = `
## Choose the mode before you choose the size

Every mode suits a long answer. With no clock on the server, the mode is a question of what your program is
doing while it waits, not of how long the answer is:

| Your program | Use |
| --- | --- |
| Shows the answer as it is written | \`"stream": true\` |
| Hands the work off and collects it later | \`"background": true\`, with [polling or a webhook](/docs/background) |
| Wants one call and one result | Synchronous — with the client timeouts below |

Whatever the mode, send an [\`Idempotency-Key\`](/docs/idempotency). A retry
that carries the same key joins the generation already running instead of
starting a second multi-hour one.
`;

const CLIENT_TIMEOUTS = `
## Client timeouts

The server will wait; the defaults of most HTTP clients will not. Every byte
the API sends resets a read timeout, so a timeout on *silence* is safe to
keep — a timeout on the *whole call* is not.

~~~python
import os
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    # None turns the read timeout off; 0 would fail every call at once.
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)
~~~

~~~typescript
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${API_BASE_URL}",
  apiKey: process.env.TECHSARA_API_KEY,
  // The largest value the SDK accepts. 0 and Infinity abort every call at once.
  timeout: 2_147_483_647,
  maxRetries: 5,
});
~~~

| Client | Setting | Why |
| --- | --- | --- |
| \`openai\` for Python | \`timeout=Timeout(None, connect=10.0)\`, \`max_retries=5\` | Its default is a 600-second limit on silence, which heartbeats already reset; \`None\` also covers a proxy of your own that buffers. Never \`0\`. |
| \`openai\` for Node | \`timeout: 2_147_483_647\`, \`maxRetries: 5\` | From version 7.5 the timeout covers the **whole** non-streamed call: with the default 600 seconds, a synchronous call of more than about half an hour fails in the SDK however healthy the generation is. |
| \`httpx\` | \`httpx.Timeout(30.0, read=None)\` | A bounded connect, no read limit. |
| \`fetch\` on Node | nothing | Its built-in 300-second header and body timers are reset by the API's bytes. Turn them off only behind a proxy of your own that goes silent. |
| \`curl\` | \`-N\`, and no \`--max-time\` | \`-N\` prints events as they arrive. |

Retries with the same \`Idempotency-Key\` attach to the running generation,
and an SDK's own automatic retry of an identical request from the same key
attaches too — but only a key makes that a guarantee:

~~~python
import uuid

answer = client.responses.create(
    model="${MODEL_ID}",
    input="Write the complete field manual, chapter by chapter.",
    max_output_tokens=1000000,
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},
)
print(answer.status, answer.max_output_tokens, answer.incomplete_details)
~~~
`;

// 2026-09-14, review of the no-timeout pages: the loop stops on every terminal
// event of CONTRACT §10.2 (`error` too, not only completed/failed), treats any
// exception from the stream but a 4xx as a dropped connection — a cut mid-body
// raises the HTTP library's RemoteProtocolError, not an SDK error, so the
// earlier `except (APIConnectionError, APITimeoutError, InternalServerError)`
// let it escape — and runs the reader's own code outside those except
// clauses, so a bug there is raised instead of spinning for ten minutes. The
// deploy sentences follow DEPLOYS_HELD (samples.ts).
const longStreamNoTimeout = (deploysHeld: boolean): string => `
## A long stream

A stream starts at once and carries an event or a heartbeat at least every
15 seconds for as long as it runs — hours, if need be. Write the text
somewhere as it arrives rather than holding it all in memory, and keep two
things: the response id from \`response.created\`, and the
\`sequence_number\` of the last event you handled. Every event carries one,
starting at 1 and going up by one. Together they are what you resume from.

~~~python
import os
import time
import uuid
from openai import OpenAI, Timeout, APIStatusError

client = OpenAI(base_url="${API_BASE_URL}", api_key=os.environ["TECHSARA_API_KEY"],
                timeout=Timeout(None, connect=10.0), max_retries=5)
key = str(uuid.uuid4())             # one key for this generation, every attempt
response_id, last_seq, final = None, 0, None

def events():
    """This generation's events after last_seq. Ends quietly when the connection drops."""
    try:
        if response_id is None:     # nothing arrived yet: the same key joins the same generation
            stream = client.responses.create(
                model="${MODEL_ID}",
                input="Write the complete field manual, chapter by chapter.",
                max_output_tokens=1000000,
                stream=True,
                extra_headers={"Idempotency-Key": key},
            )
        else:
            stream = client.responses.retrieve(response_id, stream=True, starting_after=last_seq)
        with stream:
            yield from stream
    except APIStatusError as exc:
        if exc.status_code < 500:
            raise                   # 400, 404 or 409: trying again will not change it
    except Exception:
        pass                        # the connection broke, mid-stream too: resume below

delay, give_up_at = 1.0, time.monotonic() + 600
with open("manual.txt", "a", encoding="utf-8") as out:
    while final is None:
        for event in events():      # your code runs here, never inside events()
            if event.type == "response.created":
                response_id = event.response.id
            elif event.type == "response.output_text.delta":
                out.write(event.delta)
            elif event.type == "error":
                raise RuntimeError(f"{response_id}: {event.code}: {event.message}")
            elif event.type in ("response.completed", "response.failed"):
                final = event.response
            last_seq = event.sequence_number                    # only once it was handled
            delay, give_up_at = 1.0, time.monotonic() + 600     # progress resets the budget
        if final is None:
            if time.monotonic() > give_up_at:
                raise RuntimeError(f"no progress for ten minutes on {response_id}")
            time.sleep(delay)
            delay = min(60.0, delay * 2)

print("status:", final.status, "applied:", final.max_output_tokens, "cut:", final.incomplete_details)
~~~

${deploysHeld
  ? `Behind the API's edge most interruptions are invisible, so the loop rarely
runs. It is there for the network between you and us.`
  : `The loop runs whenever the connection closes — a deploy on our side can close
it, and so can the network between you and us — and picks the generation up
where it is.`}

## Resuming a stream

A Responses stream that broke — an exception, or an end without one of its
terminal events, \`response.completed\`, \`response.failed\` or \`error\` —
is picked up with:

~~~http
GET /v1/responses/{id}?stream=true&starting_after=412
~~~

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -N "${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}?stream=true&starting_after=412" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

~~~typescript
const stream = await client.responses.retrieve(responseId, { stream: true, starting_after: lastSeq });
for await (const event of stream) {
  lastSeq = event.sequence_number;
  if (event.type === "response.output_text.delta") process.stdout.write(event.delta);
}
~~~

The API replays every event after \`starting_after\`, then follows the
generation live, and closes after the terminal event — at once, for a
response that has already finished. The rules:

* \`stream=true\` is required; \`starting_after\` without it is a \`400\`.
* Only the key that created the response — or another key of the same
  service account — can replay it. Any other key gets \`404\`, as if the
  response did not exist, even with \`responses.read\` in the same project.
* Events are kept while the generation runs and for **one hour** after it
  ends. After that, or for a response created with \`"store": false\`, the
  replay is a \`400\` on \`stream\`; \`GET /v1/responses/{id}\` without
  \`stream\` still returns a stored response.
* Retry with backoff — 1 second doubling to 60 — for at least ten minutes
  before giving up, as [the loop above](#a-long-stream) does.

**On Chat Completions** there is no response id to resume from. Retry the
same request with the same \`Idempotency-Key\`: the stream replays its chunks
from the start and continues live to \`[DONE]\`, with the same completion id.
Discard what you had written, or skip as many characters as you already have.
`;

const longBackgroundNoTimeout = (deploysHeld: boolean): string => `
## A long background job

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: aaaa-bbbb-cccc-dddd" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Write the complete field manual, chapter by chapter.",
    "max_output_tokens": 1000000,
    "background": true
  }'
~~~

The \`202\` carries the planned \`max_output_tokens\`. Then poll
\`GET /v1/responses/{id}\` slowly — once a minute is plenty for a job measured
in hours — or let a [webhook](/docs/webhooks) tell you it finished. The stored
response keeps the whole text for your project's retention window, and a
background response can be streamed too, with the same
[resume](#resuming-a-stream) request.

* **It may wait before it starts**, for as long as the engine is busy: the
  response stays \`queued\` and no clock turns the wait into a failure.
* **A service restart does not end it.** A running generation is written
  ahead as it goes and carries on after a deploy from where it stopped.
  Neither disconnecting nor closing your program cancels a background job —
  only \`POST /v1/responses/{id}/cancel\` does.

## What still ends a generation

Besides finishing, reaching its \`max_output_tokens\`, or being cancelled:

* **The engine is down for 30 minutes without a break.** Shorter outages,
  restarts and recoveries are waited out.
* **Three attempts in a row make no progress** while the engine is otherwise
  serving.
* **The same generation is caught up in two engine crashes.** It fails with
  \`x-should-retry: false\`: a request that twice coincided with a crash is
  not sent a third time automatically.
* **The key is revoked** while it runs.
${deploysHeld
  ? `* **The service stays unreachable for 30 minutes** while your connection is
  held open waiting for it.`
  : `* **The service stays unreachable** for longer than your client keeps
  retrying. The connection is closed, and the generation is then treated as
  abandoned, as below.`}
* **You go away and do not come back.** A synchronous call or stream whose
  client disconnects is kept for **10 minutes** when it carries an
  \`Idempotency-Key\` or is a Responses stream, and for **2 minutes**
  otherwise, so that a retry or a resume can find it; then it is cancelled.
  A background job is never cancelled this way.
* **\`"store": false\`** opts out of everything above that needs the
  generation written down: such a request is cancelled when its client
  disconnects, ends if the service restarts under it, and cannot be resumed.

## What is kept while it runs

To make resuming possible, a generation's request and the events it streams
are stored while it runs, and the events for one hour after it ends. Only the
key that created the response, or a key of the same service account, can read
them back as a stream. \`"store": false\` keeps nothing — at the price listed
above.
`;

/**
 * The page in either state. Exported so tests/docs-files.test.tsx can render
 * and check the no-timeout page before it is live; the site reads
 * `longOutput`, built from the switch.
 */
export function longOutputPage({
  noTimeout,
  deploysHeld = DEPLOYS_HELD,
}: {
  noTimeout: boolean;
  /** Whether a run through the public URL proved deploys are held (samples.ts). */
  deploysHeld?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [
        INTRO_NO_TIMEOUT,
        CEILING,
        SHARED_WINDOW,
        RESPONSE_FIELDS,
        HOW_LONG,
        noWallClock(deploysHeld),
        EVERY_MODE,
        CLIENT_TIMEOUTS,
        longStreamNoTimeout(deploysHeld),
        longBackgroundNoTimeout(deploysHeld),
      ]
    : [
        INTRO_CURRENT,
        CEILING,
        SHARED_WINDOW,
        RESPONSE_FIELDS,
        HOW_LONG,
        WALL_CLOCK,
        CHOOSE_MODE,
        LONG_STREAM,
        LONG_BACKGROUND,
      ];
  return {
    slug: 'long-output',
    title: 'Long outputs',
    summary: noTimeout
      ? 'Up to 1,000,000 output tokens on techsara-35b: how the shared window ' +
        'clamps a request, how long it takes, and the client settings that let it finish.'
      : 'Up to 1,000,000 output tokens on techsara-35b: how the shared window ' +
        'clamps a request, how long it takes, and why only streaming or background suits it.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    // Each section is written with its own leading and trailing newline;
    // exactly one blank line goes between two of them.
    body: sections.map((section) => section.trim()).join('\n\n'),
  };
}

export const longOutput: DocPage = longOutputPage({ noTimeout: NO_TIMEOUT_LIVE });
