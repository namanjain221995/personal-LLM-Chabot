import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';

export const responses: DocPage = {
  slug: 'responses',
  title: 'The Responses API',
  summary:
    'POST /v1/responses is the generation endpoint: synchronous, streaming ' +
    'or background, with every field validated rather than quietly ignored.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
## Create a response

~~~http
POST /v1/responses
~~~

Requires the \`responses.write\` scope.

~~~json
{
  "model": "${MODEL_ID}",
  "input": "Explain retrieval-augmented generation.",
  "instructions": "Answer in British English.",
  "stream": true,
  "background": false,
  "max_output_tokens": 1000,
  "temperature": 0.2,
  "metadata": { "customer_request_id": "abc-123" }
}
~~~

### The fields

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. A public model id your key may use. Otherwise \`404 model_not_found\`. |
| \`input\` | string, or a list of messages | Required, non-empty. Over the model's input ceiling is \`400 context_length_exceeded\`. |
| \`instructions\` | string | Optional. Must not be blank when present. |
| \`stream\` | boolean | Default \`false\`. See [streaming](/docs/streaming). |
| \`background\` | boolean | Default \`false\`. See [background responses](/docs/background). |
| \`max_output_tokens\` | integer | Optional, at least 1, at most the model's ceiling. |
| \`temperature\` | number | Optional, \`0.0\` to \`2.0\`. Defaults to \`0.2\`, which is what the chat application asks for. |
| \`metadata\` | object of strings | Optional. At most 16 keys; keys ≤ 64 characters, values ≤ 512 characters; strings only. |

The whole body must be at most **1 MiB**, which is checked before it is
parsed — over it is \`413 request_too_large\`.

\`stream\` and \`background\` cannot both be true. A background response is
delivered by \`GET /v1/responses/{id}\` and by a webhook; there is no stream
to attach to, so honouring \`stream\` would be a lie and ignoring it would be
a silent drop.

### Messages

\`input\` is either a plain string — the shorthand for a single user turn — or
an explicit conversation:

~~~json
{
  "model": "${MODEL_ID}",
  "input": [
    { "role": "system", "content": "You are a concise technical writer." },
    { "role": "user", "content": "Summarise our uptime policy." },
    { "role": "assistant", "content": "Which policy document should I use?" },
    { "role": "user", "content": "The 2026 one." }
  ]
}
~~~

The roles are \`system\`, \`user\` and \`assistant\`, and \`content\` is a
string. \`instructions\`, when you send it, becomes the first system message,
ahead of anything in \`input\`.

The API is stateless: it remembers nothing between requests. A conversation
is whatever you send in \`input\`.

### A parameter we cannot honour is rejected

This is the rule most likely to surprise you, and it is deliberate. Unknown
and unsupported fields — \`top_p\`, \`n\`, \`seed\`, \`logit_bias\`, \`tools\`,
\`tool_choice\`, \`response_format\` and the rest — produce a \`400\` naming
the field, rather than an answer that ignored the knob you were relying on.

~~~json
{
  "error": {
    "message": "Extra inputs are not permitted",
    "type": "invalid_request_error",
    "code": "invalid_request_error",
    "param": "top_p",
    "request_id": "req_…"
  }
}
~~~

Accepting and dropping a sampling parameter is what makes an API
untrustworthy: the caller cannot tell it happened. When you port a client,
expect to delete fields; the error names each one.

Nothing in the body can change your project, workspace, key, model target,
limits or audit policy either. Those come from the key, and a hopeful
\`"workspace_id"\` in the body is just another \`400\`.

### \`max_output_tokens\`, precisely

* Send a value: it must be between 1 and the model's ceiling. Above it is a
  \`400\` — we will not silently clamp an explicit 100,000 down and then bill
  you for an answer you believe is complete.
* Send nothing: you get the platform default of **8,192**, clamped to the
  model's ceiling if that is lower.

## The response

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789200000,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [{ "type": "output_text", "text": "…" }]
    }
  ],
  "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 }
}
~~~

| Field | Notes |
| --- | --- |
| \`id\` | \`resp_\` and 24 hex characters. Keep it: it is how you read, cancel and correlate the response. |
| \`status\` | One of \`queued\`, \`in_progress\`, \`completed\`, \`failed\`, \`cancelled\`. |
| \`output\` | A list, always. One assistant message today; it stays a list so a future item kind does not change the shape. |
| \`usage\` | \`null\` when the engine reported no counts. Never \`0\` for "not measured". |
| \`error\` | Present only on a failed response, carrying the same \`code\` vocabulary as the HTTP envelope. |

Read the text at
\`output[0].content[0].text\`, and write your client so an empty \`output\`
list is not a crash.

## Read one back

~~~http
GET /v1/responses/{id}
~~~

Requires \`responses.read\`. Project-scoped: a response created by another
project is \`404 response_not_found\`, exactly like one that never existed.

Every response gets a durable record, whichever mode created it, so this works
for all three — but only a [background response](/docs/background) keeps its
generated text. A synchronous or streamed response reads back with its
\`status\`, its \`usage\` and, on a failure, its \`error\`, and with an empty
\`output\` list: the text went to you once and was not stored. That empty list
is the honest answer, not a bug to work around.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

## Cancel one

~~~http
POST /v1/responses/{id}/cancel
~~~

Requires \`responses.write\`, and is **idempotent** — cancelling an
already-finished or already-cancelled response is not an error, so a retry
after a network failure is safe. Like every call that presents a key, it
counts against the project's [rate limits](/docs/rate-limits).

~~~bash
curl -X POST ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}/cancel \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

## Safe retries

Give any \`POST /v1/responses\` you might retry an
[\`Idempotency-Key\`](/docs/idempotency). Without one, a retry after a
timeout is a second generation: a second bill, and possibly a second answer
your user sees.
`.trim(),
};
