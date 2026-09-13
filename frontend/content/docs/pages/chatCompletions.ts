import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const chatCompletions: DocPage = {
  slug: 'chat-completions',
  title: 'Chat Completions compatibility',
  summary:
    'A deliberately compatible endpoint so an existing client can point at ' +
    'this platform with a base-URL change.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
~~~http
POST /v1/chat/completions
~~~

Requires the \`responses.write\` scope. Same engine, same limits, same error
envelope, same idempotency rules as [\`/v1/responses\`](/docs/responses).

## Why it exists

\`/v1/chat/completions\` accepts the widely used chat-completions request
shape — a \`model\`, a \`messages\` array of \`{role, content}\` turns, and the
usual streaming and sampling switches — because a great many client libraries
and internal tools already speak it. Pointing one of them at this platform
should be a base URL and a key, not a rewrite.

We are compatible with the *shape*. We are not a re-implementation of anyone
else's product, and this documentation is our own: where behaviour differs it
is written down here, in plain words, rather than left for you to discover.

**Build new work against [the Responses API](/docs/responses).** It is the
surface this platform develops; compatibility is for code you already have.

## The fields this endpoint accepts

| Field | Notes |
| --- | --- |
| \`model\` | Required. \`${MODEL_ID}\`. |
| \`messages\` | Required. \`{role, content}\` turns; \`role\` is \`system\`, \`user\` or \`assistant\` and \`content\` is a string. |
| \`stream\` | Optional. See below. |
| \`max_tokens\` | Optional. The older spelling of \`max_output_tokens\`, and mapped to it. |
| \`temperature\` | Optional, \`0.0\`–\`2.0\`. |
| \`stream_options\` | Optional, and only \`{"include_usage": true}\`. |

That is the complete list. **Any other field is a \`400\`** —
\`"Unsupported field: top_p."\`, with the field in \`param\`. The same goes
for an unknown key inside \`stream_options\`.

There is no \`background\` here: background work is a
[Responses](/docs/background) feature, and this endpoint has no shape to
express it.

## What is different, and will be different

* **Unsupported parameters are rejected, not ignored.** The rule holds
  everywhere on \`/v1\`: if this platform cannot honour a field, you get a
  \`400\` naming it. Porting a client usually means deleting a few fields.
* **The model list is short.** \`${MODEL_ID}\` is the id; a model your key may
  not use is \`404\`, never \`403\`.
* **Content is text.** A message's \`content\` is a string. Multimodal part
  lists are not accepted.
* **No tools.** See [tool calling](/docs/tools).
* **Errors are TechSara's envelope.** \`code\`, \`type\`, \`param\`,
  \`request_id\` — see [errors](/docs/errors).

## A request

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "messages": [
      {"role": "system", "content": "You are a concise technical writer."},
      {"role": "user", "content": "Summarise our uptime policy."}
    ]
  }'
~~~

## The response

~~~json
{
  "id": "chatcmpl_…",
  "object": "chat.completion",
  "created": 1789200000,
  "model": "${MODEL_ID}",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "…" },
      "finish_reason": "stop"
    }
  ],
  "usage": { "prompt_tokens": 37, "completion_tokens": 112, "total_tokens": 149 }
}
~~~

\`usage\` keeps the older \`prompt_tokens\` / \`completion_tokens\` names
here, because that is what a client of this shape reads. It is still
\`null\` — never \`0\` — when the engine reported no counts.

## Streaming

Set \`"stream": true\`. This endpoint uses the chat-completions streaming
dialect, which is **not** the Responses event grammar:

* chunks are anonymous \`data:\` lines with no \`event:\` name and no
  sequence number, each one an object whose \`object\` is
  \`chat.completion.chunk\`;
* the first chunk's delta announces the assistant role;
* a chunk carrying \`finish_reason\` says why generation ended — \`stop\`, or
  \`length\` when the answer hit its token ceiling;
* the stream ends with the literal line \`data: [DONE]\`;
* with \`"stream_options": {"include_usage": true}\`, exactly one extra chunk
  goes out before \`[DONE]\`, with an empty \`choices\` array and the usage
  totals. Without that option there is no such chunk — several clients crash
  on a choice-less chunk they did not ask for.

~~~text
data: {"id":"…","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Ret"},"finish_reason":null}],"usage":null}

data: {"id":"…","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"rieval"},"finish_reason":null}],"usage":null}

data: {"id":"…","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":null}

data: [DONE]
~~~

If generation fails after the stream has started, one chunk with an empty
\`choices\` array carries the failure in an \`error\` object — the same
\`code\` the HTTP envelope would have used — and \`data: [DONE]\` still
follows, because a client reading this dialect waits for it:

~~~text
data: {"id":"…","object":"chat.completion.chunk","choices":[],"usage":null,"error":{"message":"The model is not available at the moment.","type":"service_unavailable_error","code":"model_unavailable","param":null}}

data: [DONE]
~~~

A heartbeat comment (\`: ping\`) may appear between chunks so an idle proxy
does not close the connection. Every conforming SSE parser drops comments;
if you wrote your own, drop them too.

## Idempotency

\`Idempotency-Key\` works here exactly as it does on \`/v1/responses\`. See
[idempotency](/docs/idempotency).

## Migrating

[Migration and compatibility](/docs/migration) is the page-by-page version of
this list, including what to change in an OpenAI-shaped client and what to
expect the first time it refuses a parameter.
`.trim(),
};
