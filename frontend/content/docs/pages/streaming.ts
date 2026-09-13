import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';

export const streaming: DocPage = {
  slug: 'streaming',
  title: 'Streaming',
  summary:
    'Server-sent events from /v1/responses: a numbered lifecycle, one ' +
    'terminal event, and a heartbeat so nothing idle gets cut.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
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
  than deciding the connection is dead.
* \`response.failed\` and \`error\` — the two failure terminals.

Each frame is an \`event:\` line naming the event and a \`data:\` line with a
JSON object. The object repeats the name in \`type\` and carries a
\`sequence_number\`.

~~~text
event: response.created
data: {"type":"response.created","sequence_number":1,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"queued","model":"${MODEL_ID}","output":[],"usage":null}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","sequence_number":3,"item_id":"msg_…","output_index":0,"content_index":0,"delta":"Retrieval"}

event: response.output_text.done
data: {"type":"response.output_text.done","sequence_number":42,"item_id":"msg_…","output_index":0,"content_index":0,"text":"Retrieval-augmented generation …"}

event: response.completed
data: {"type":"response.completed","sequence_number":43,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"completed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented generation …"}]}],"usage":{"input_tokens":37,"output_tokens":112,"total_tokens":149}}}
~~~

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

## Heartbeats

A comment frame goes out at least every 15 seconds while nothing else is
happening:

~~~text
: ping
~~~

It keeps idle proxies from closing a long generation. It is a comment, not an
event: it carries no sequence number and every conforming parser drops it. If
you hand-rolled a parser, make sure a line starting with \`:\` is ignored
rather than fed to \`JSON.parse\`.

## Errors mid-stream

The status line is committed as soon as streaming starts, so a failure after
that is delivered **in the stream**, not as an HTTP status. On
\`/v1/responses\` it is a \`response.failed\` event: the response object again,
with \`status\` \`failed\`, whatever text had already been generated, the
usage that was spent, and an \`error\` naming the code.

~~~text
event: response.failed
data: {"type":"response.failed","sequence_number":7,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"failed","model":"${MODEL_ID}","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Retrieval-augmented"}]}],"usage":null,"error":{"code":"model_recovering","message":"The model is restarting. This request is safe to retry."}}}
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

## When a stream is the wrong tool

If your client cannot hold a connection for the length of the work — a
serverless function with a short ceiling, a mobile app on a flaky network —
use a [background response](/docs/background) and a
[webhook](/docs/webhooks) instead.

There is no resume. A dropped stream cannot be replayed, and the text of a
non-background response is not retained after it ends — so a stream you
cannot hold is work you will have to pay for twice. Background responses are
durable precisely because that trade is sometimes the wrong one.
`.trim(),
};
