import type { DocPage } from '../types';
import {
  API_BASE_URL,
  DEPLOYS_HELD,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): a Responses stream gains the
// item and content-part events both SDKs' stream helpers wait for, no longer
// ends on a wall clock, waits for capacity inside the stream, and can be
// resumed by response id and sequence number. The page is built in either
// state from NO_TIMEOUT_LIVE, so it never describes a resume the running API
// cannot do.

const INTRO = `
Send \`"stream": true\` to \`POST /v1/responses\` and the answer arrives as
server-sent events instead of one body at the end. First token in about a
second beats a complete answer in thirty, every time, for anything a person
is waiting on.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -N ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${MODEL_ID}", "input": "Explain RAG.", "stream": true}'
~~~

\`-N\` disables curl's own buffering. Without it you will watch nothing
happen and then everything at once.
`;
const S_RESPONSE_HEADERS = `
## Response headers

~~~http
Content-Type: text/event-stream
Cache-Control: no-store, no-cache, no-transform
X-Accel-Buffering: no
Connection: keep-alive
~~~

There is no \`Content-Length\`. If you put a proxy in front of your client,
turn its response buffering off, or it will hold the whole stream and hand
you one lump — which is exactly the bug \`X-Accel-Buffering: no\` exists to
prevent upstream of you.
`;
const S_THE_EVENT_SEQUENCE = `
## The event sequence

~~~text
response.created
response.in_progress
response.output_text.delta   (many)
response.output_text.done
response.completed
~~~

with two more that can appear:

* \`response.queued\` — the engine is recovering and your request is allowed
  to wait. It is emitted so you can see *why* nothing is arriving, rather
  than deciding the connection is dead. Only \`${MODEL_ID}\` sends it.
* \`response.failed\` and \`error\` — the two failure terminals.

Each frame is an \`event:\` line naming the event and a \`data:\` line with a
JSON object. The object repeats the name in \`type\` and carries a
\`sequence_number\`.

~~~text
event: response.created
data: {"type":"response.created","sequence_number":1,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"queued","model":"${MODEL_ID}","output":[],"max_output_tokens":8192,"incomplete_details":null,"usage":null}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_…","output_index":0,"content_index":0,"delta":"Retrieval"}

event: response.output_text.done
data: {"type":"response.output_text.done","sequence_number":42,"item_id":"msg_…","output_index":0,"content_index":0,"text":"Retrieval-augmented generation …"}

event: response.completed
data: {"type":"response.completed","sequence_number":43,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"completed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented generation …"}]}],"max_output_tokens":8192,"incomplete_details":null,"usage":{"input_tokens":37,"output_tokens":112,"total_tokens":149}}}
~~~
`;
const S_FOUR_GUARANTEES_YOU_CAN_CODE_AGAINST = `
## Four guarantees you can code against

1. **\`sequence_number\` starts at 1 and increases by exactly 1.** A gap means
   you dropped or reordered a frame. Check it; that is what it is for.
2. **Exactly one terminal event.** \`response.completed\`, \`response.failed\`
   or \`error\` — one of them, once, and nothing after it.
3. **\`usage\` only on the terminal event.** Every earlier event carries
   \`usage: null\`, present and explicitly null, so nobody is tempted to sum
   deltas into a bill.
4. **\`response.output_text.done\` carries the whole text.** If you lost a
   delta, recover the answer from there instead of asking for the response
   again.

The object inside \`response.completed\` is byte for byte the body a
non-streaming request would have returned, so one parser serves both modes.
`;
const S_THE_APPLIED_OUTPUT_CEILING = `
## The applied output ceiling

Every response object in the stream carries \`max_output_tokens\` and
\`incomplete_details\`. On \`response.created\` and \`response.in_progress\`
\`max_output_tokens\` is the ceiling the server **planned** from your request —
already clamped to what your prompt leaves in the context window. On the
terminal event it is the exact ceiling **applied**, which on \`techsara-35b\`
can be lower or higher once the engine has counted your prompt precisely (the
plan counts about three characters per token; English prose is nearer four).
\`incomplete_details\` is
\`{"reason": "max_output_tokens"}\` on the terminal event when the answer
stopped because it reached that ceiling, and \`null\` otherwise. See
[long outputs](/docs/long-output).
`;
const S_HEARTBEATS = `
## Heartbeats

A comment frame goes out at least every 15 seconds while nothing else is
happening:

~~~text
: ping
~~~

It keeps idle proxies from closing a long generation, and it keeps going for
the whole life of the stream on every model — through a slow first token on a
long prompt, and through an answer that runs for hours. It is a comment, not an
event: it carries no sequence number and every conforming parser drops it. If
you hand-rolled a parser, make sure a line starting with \`:\` is ignored
rather than fed to \`JSON.parse\`.

The heartbeat covers the network. Your own client's read timeout is the one
thing it cannot reach: leave it off for a stream (\`timeout=None\` in the Python
sample below), or set it well above 15 seconds.
`;
const S_ERRORS_MID_STREAM = `
## Errors mid-stream

The status line is committed as soon as streaming starts, so a failure after
that is delivered **in the stream**, not as an HTTP status. On
\`/v1/responses\` it is a \`response.failed\` event: the response object again,
with \`status\` \`failed\`, whatever text had already been generated, the
usage that was spent, and an \`error\` naming the code.

~~~text
event: response.failed
data: {"type":"response.failed","sequence_number":7,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"failed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented"}]}],"max_output_tokens":8192,"incomplete_details":null,"usage":null,"error":{"code":"model_recovering","message":"The model is restarting. This request is safe to retry."}}}
~~~

A generation that reaches its wall clock ends with a \`response.failed\` too, with the code
\`timeout\` — and still carries everything written up to that moment:

~~~text
event: response.failed
data: {"type":"response.failed","sequence_number":406213,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"failed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Chapter 1 …"}]}],"max_output_tokens":1000000,"incomplete_details":null,"usage":{"input_tokens":412,"output_tokens":812344,"total_tokens":812756},"error":{"code":"timeout","message":"The response took longer than the 20900 second limit."}}}
~~~

The grammar reserves one more terminal, \`error\`, for a failure that has no
response object to describe. Its fields sit at the top level rather than
inside an envelope:

~~~text
event: error
data: {"type":"error","sequence_number":1,"code":"model_unavailable","message":"The model is not available at the moment.","param":null}
~~~

Neither repeats the request id: it arrived as \`X-Request-Id\` before the
first byte. Both use the same codes as the [HTTP envelope](/docs/errors), so
one retry table covers both modes. A stream can also open on a failure
terminal if the request dies between the \`200\` and the first lifecycle
event.
`;
const S_READING_A_STREAM = `
## Reading a stream

~~~python
import json
import os
import httpx

headers = {"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"}
payload = {"model": "${MODEL_ID}", "input": "Explain RAG.", "stream": True}

with httpx.stream(
    "POST", "${API_BASE_URL}/responses",
    headers=headers, json=payload, timeout=None,
) as response:
    response.raise_for_status()
    event = None
    for line in response.iter_lines():
        if not line:                      # blank line ends one frame
            event = None
            continue
        if line.startswith(":"):          # heartbeat comment
            continue
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
            if event == "response.output_text.delta":
                print(data["delta"], end="", flush=True)
            elif event == "response.completed":
                print()
                print("usage:", data["response"]["usage"])
            elif event == "response.failed":
                raise RuntimeError(data["response"]["error"]["code"])
            elif event == "error":
                raise RuntimeError(data["code"])
~~~

~~~typescript
const response = await fetch(\`\${BASE_URL}/responses\`, {
  method: "POST",
  headers: {
    Authorization: \`Bearer \${apiKey}\`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ model: "${MODEL_ID}", input: "Explain RAG.", stream: true }),
});

if (!response.ok || !response.body) {
  throw new Error(\`stream did not start: \${response.status}\`);
}

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });

  // Frames are separated by a blank line. Keep the unfinished tail.
  const frames = buffer.split("\\n\\n");
  buffer = frames.pop() ?? "";

  for (const frame of frames) {
    let name = "";
    let data = "";
    for (const line of frame.split("\\n")) {
      if (line.startsWith(":")) continue;               // heartbeat
      if (line.startsWith("event: ")) name = line.slice(7);
      else if (line.startsWith("data: ")) data += line.slice(6);
    }
    if (!data) continue;
    const payload = JSON.parse(data);
    if (name === "response.output_text.delta") process.stdout.write(payload.delta);
    if (name === "response.completed") console.log(payload.response.usage);
    if (name === "response.failed") throw new Error(payload.response.error.code);
    if (name === "error") throw new Error(payload.code);
  }
}
~~~
`;
const S_WHEN_A_STREAM_IS_THE_WRONG_TOOL = `
## When a stream is the wrong tool

If your client cannot hold a connection for the length of the work — a
serverless function with a short ceiling, a mobile app on a flaky network, an
answer of hundreds of thousands of tokens — use a
[background response](/docs/background) and a [webhook](/docs/webhooks)
instead.

There is no resume. A dropped stream cannot be replayed, and the text of a
non-background response is not retained after it ends — so a stream you
cannot hold is work you will have to pay for twice. Background responses are
durable precisely because that trade is sometimes the wrong one.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_EVENT_SEQUENCE = `
## The event sequence

~~~text
response.created
response.queued                 (only while it waits for its engine)
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta      (many)
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
~~~

\`response.queued\` appears when the request has to wait for its engine — busy,
or recovering — on any model, and heartbeats follow until the work starts,
however long that is: a wait is never turned into an error. The two failure
terminals, \`response.failed\` and \`error\`, can take the place of
\`response.completed\`.

The item and content-part events frame the answer the way both official client
libraries' stream helpers expect: one assistant message holding one text part.

Each frame is an \`event:\` line naming the event and a \`data:\` line with a
JSON object. The object repeats the name in \`type\` and carries a
\`sequence_number\`. There are never \`id:\` or \`retry:\` lines: a stream is
resumed by [sequence number](#resuming-a-stream), not by an automatic SSE
reconnect.

~~~text
event: response.created
data: {"type":"response.created","sequence_number":1,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"queued","model":"${MODEL_ID}","output":[],"max_output_tokens":8192,"incomplete_details":null,"usage":null}}

event: response.output_item.added
data: {"type":"response.output_item.added","sequence_number":3,"output_index":0,"item":{"id":"msg_…","type":"message","role":"assistant","status":"in_progress","content":[]}}

event: response.content_part.added
data: {"type":"response.content_part.added","sequence_number":4,"item_id":"msg_…","output_index":0,"content_index":0,"part":{"type":"output_text","text":"","annotations":[]}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":5,"item_id":"msg_…","output_index":0,"content_index":0,"delta":"Retrieval"}

event: response.output_text.done
data: {"type":"response.output_text.done","sequence_number":44,"item_id":"msg_…","output_index":0,"content_index":0,"text":"Retrieval-augmented generation …"}

event: response.content_part.done
data: {"type":"response.content_part.done","sequence_number":45,"item_id":"msg_…","output_index":0,"content_index":0,"part":{"type":"output_text","text":"Retrieval-augmented generation …","annotations":[]}}

event: response.output_item.done
data: {"type":"response.output_item.done","sequence_number":46,"output_index":0,"item":{"id":"msg_…","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"Retrieval-augmented generation …","annotations":[]}]}}

event: response.completed
data: {"type":"response.completed","sequence_number":47,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"completed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented generation …"}]}],"max_output_tokens":8192,"incomplete_details":null,"usage":{"input_tokens":37,"output_tokens":112,"total_tokens":149}}}
~~~
`;
const LATER_HEARTBEATS = `
## Heartbeats

A comment frame goes out at least every 15 seconds while nothing else is
happening:

~~~text
: ping
~~~

It keeps idle proxies from closing a long generation, and it keeps going for
the whole life of the stream on every model — through a wait for the engine,
through a slow first token on a long prompt, and through an answer that runs
for hours. It is a comment, not an event: it carries no sequence number and
every conforming parser drops it. If you hand-rolled a parser, make sure a line
starting with \`:\` is ignored rather than fed to \`JSON.parse\`.

The heartbeat covers the network. Your own client's timeout is the one thing it
cannot reach: [timeouts](/docs/timeouts) has the exact settings for each
client — \`timeout=None\` in Python, never \`0\`.
`;
const LATER_ERRORS_MID_STREAM = `
## Errors mid-stream

The status line is committed as soon as streaming starts, so a failure after
that is delivered **in the stream**, not as an HTTP status. On
\`/v1/responses\` it is a \`response.failed\` event: the response object again,
with \`status\` \`failed\`, whatever text had already been generated, the
usage that was spent, and an \`error\` naming the code.

~~~text
event: response.failed
data: {"type":"response.failed","sequence_number":7,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"failed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented"}]}],"max_output_tokens":8192,"incomplete_details":null,"usage":null,"error":{"code":"model_unavailable","message":"The model is not available at the moment."}}}
~~~

A stream is not failed for taking a long time: there is no wall clock. It fails
when the engine stays down for 30 minutes, when the same generation is caught
in two engine crashes, or when the key is revoked — the full list is under
[timeouts](/docs/timeouts#what-still-ends-a-request).

The grammar reserves one more terminal, \`error\`, for a failure that has no
response object to describe. Its fields sit at the top level rather than
inside an envelope:

~~~text
event: error
data: {"type":"error","sequence_number":1,"code":"model_unavailable","message":"The model is not available at the moment.","param":null}
~~~

Neither repeats the request id: it arrived as \`X-Request-Id\` before the
first byte. Both use the same codes as the [HTTP envelope](/docs/errors), so
one retry table covers both modes.

A connection that simply **breaks** — no terminal event at all — is not a
failure of the response. The generation carries on; [resume it](#resuming-a-stream).
`;
// 2026-09-14, review: the deploy sentence is printed only once a run through the
// public URL proved deploys are held (DEPLOYS_HELD, samples.ts).
const laterWhenAStreamIsTheWrongTool = (deploysHeld: boolean): string => `
## Resuming a stream

Keep the response id from \`response.created\` and the \`sequence_number\` of
the last event you handled. When the stream breaks — an exception, or an end
with no \`response.completed\`, \`response.failed\` or \`error\` — ask for the
rest:

~~~bash
curl -N "${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}?stream=true&starting_after=46" \
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

The API replays every event after \`starting_after\`, follows the generation
live, and closes after the terminal event. Only the key that created the
response — or another key of its service account — can replay it; any other key
gets \`404\`. Events are kept for one hour after the response ends, and never
for a request sent with \`"store": false\`. [Timeouts](/docs/timeouts#resuming-a-stream)
has the retry loop, in Python and in TypeScript.

${deploysHeld
  ? `Behind the API's edge, most interruptions — including our own deploys — are
invisible, so the loop rarely runs. It is there for the network between you and
us.`
  : `A deploy on our side can close a connected stream, and so can the network
between you and us. Neither ends the generation: it carries on, and this request
picks it up where you left it.`}

## When a stream is the wrong tool

If nothing is watching the answer as it is written — a batch job, a serverless
function that hands the work off — use a [background response](/docs/background)
and a [webhook](/docs/webhooks) instead. A background response can still be
streamed later, with the same resume request.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function streamingPage({
  noTimeout,
  deploysHeld = DEPLOYS_HELD,
}: {
  noTimeout: boolean;
  deploysHeld?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [INTRO, S_RESPONSE_HEADERS, LATER_THE_EVENT_SEQUENCE, S_FOUR_GUARANTEES_YOU_CAN_CODE_AGAINST, S_THE_APPLIED_OUTPUT_CEILING, LATER_HEARTBEATS, LATER_ERRORS_MID_STREAM, S_READING_A_STREAM, laterWhenAStreamIsTheWrongTool(deploysHeld)]
    : [INTRO, S_RESPONSE_HEADERS, S_THE_EVENT_SEQUENCE, S_FOUR_GUARANTEES_YOU_CAN_CODE_AGAINST, S_THE_APPLIED_OUTPUT_CEILING, S_HEARTBEATS, S_ERRORS_MID_STREAM, S_READING_A_STREAM, S_WHEN_A_STREAM_IS_THE_WRONG_TOOL];
  return {
    slug: 'streaming',
    title: 'Streaming',
    summary: noTimeout
      ? 'Server-sent events from /v1/responses: a numbered lifecycle, one ' +
        'terminal event, a heartbeat so nothing idle gets cut, and a resume by sequence number.'
      : 'Server-sent events from /v1/responses: a numbered lifecycle, one ' +
      'terminal event, and a heartbeat so nothing idle gets cut.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const streaming: DocPage = streamingPage({ noTimeout: NO_TIMEOUT_LIVE });
