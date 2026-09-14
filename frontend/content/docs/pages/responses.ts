import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
  OCR_MODEL_ID,
  VISION_MODEL_ID,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): `stream` and `background` may be
// combined, `store` opts a request out of being written down, a synchronous
// request is no longer unsuitable for long work, and a response can be
// streamed back by id. Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `

`;
const S_CREATE_A_RESPONSE = `
## Create a response

~~~http
POST /v1/responses
~~~

Requires the \`responses.write\` scope. Three models generate here —
\`${MODEL_ID}\`, \`${VISION_MODEL_ID}\` and \`${OCR_MODEL_ID}\` — and one
scope covers all of them; which of them your key may use is your project's
model allowlist. The other models have endpoints of their own: see the
[model reference](/docs/models).

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
`;
const S_THE_FIELDS = `
### The fields

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. A chat model id your key may use. Otherwise \`404 model_not_found\`; a model of another kind, such as an embeddings model, is a \`400\` naming \`model\`. |
| \`input\` | string, or a list of messages | Required, non-empty. Over the model's input ceiling is \`400 context_length_exceeded\`. A \`user\` message may carry [images](/docs/images). |
| \`instructions\` | string | Optional. Must not be blank when present. |
| \`stream\` | boolean | Default \`false\`. See [streaming](/docs/streaming). |
| \`background\` | boolean | Default \`false\`. See [background responses](/docs/background). |
| \`max_output_tokens\` | integer | Optional, at least 1, at most the model's ceiling — 1,000,000 on \`${MODEL_ID}\`. Clamped to what your prompt leaves in the window; see below. |
| \`temperature\` | number | Optional, \`0.0\` to \`2.0\`. Defaults to \`0.2\`, which is what the chat application asks for — except on \`${OCR_MODEL_ID}\`, where it defaults to \`0.0\`. |
| \`metadata\` | object of strings | Optional. At most 16 keys; keys ≤ 64 characters, values ≤ 512 characters; strings only. |

The whole body must be at most **20 MiB** — room for images — and all of the
*text* in it together at most **1 MiB**. The body size is checked before it is
parsed; either one over is \`413 request_too_large\`.

\`stream\` and \`background\` cannot both be true. A background response is
delivered by \`GET /v1/responses/{id}\` and by a webhook; there is no stream
to attach to, so honouring \`stream\` would be a lie and ignoring it would be
a silent drop.
`;
const S_MESSAGES = `
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
string — or, on a \`user\` message to a model that accepts images, a list of
\`input_text\` and \`input_image\` parts:

~~~json
{
  "model": "${VISION_MODEL_ID}",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "What does this chart show?" },
        { "type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo…" }
      ]
    }
  ]
}
~~~

Images are sent as \`data:\` URLs only, never as links; the rules and limits
are on [images and OCR](/docs/images). \`instructions\`, when you send it,
becomes the first system message, ahead of anything in \`input\`.

The API is stateless: it remembers nothing between requests. A conversation
is whatever you send in \`input\`.
`;
const S_A_PARAMETER_WE_CANNOT_HONOUR_IS_REJECTED = `
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
`;
const S_MAX_OUTPUT_TOKENS_PRECISELY = `
### \`max_output_tokens\`, precisely

* Send nothing: you get the platform default of **8,192**, clamped to the
  model's ceiling if that is lower.
* Send a value above the model's ceiling — 1,000,000 on \`${MODEL_ID}\`,
  24,576 on \`${VISION_MODEL_ID}\`, 8,192 on \`${OCR_MODEL_ID}\` — or above your
  project's own ceiling: a \`400\`. That value could never be honoured.
* Send a value within the ceiling that is more than your prompt leaves in the
  context window: it is **clamped** to the room that is left. Input and output
  share the window, so this is the only answer that is both honest and useful.
* Either way the response says what was applied, in its own
  \`max_output_tokens\` field, and \`incomplete_details\` says whether the
  answer stopped because it reached it. Nothing is clamped silently.

A long answer takes a long time — a million tokens is hours — and a
synchronous request that runs past about 100 seconds can be cut off on the way
to you. Above about 5,000 output tokens, [stream](/docs/streaming) or use
[background](/docs/background). [Long outputs](/docs/long-output) has the
numbers.
`;
const S_THE_RESPONSE = `
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
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 }
}
~~~

| Field | Notes |
| --- | --- |
| \`id\` | \`resp_\` and 24 hex characters. Keep it: it is how you read, cancel and correlate the response. |
| \`status\` | One of \`queued\`, \`in_progress\`, \`completed\`, \`failed\`, \`cancelled\`. |
| \`output\` | A list, always. One assistant message today; it stays a list so a future item kind does not change the shape. |
| \`max_output_tokens\` | The output ceiling **applied** to this generation, after any clamping. \`null\` only on a response created before 2026-09-13. |
| \`incomplete_details\` | \`{"reason": "max_output_tokens"}\` when the answer stopped because it reached that ceiling, otherwise \`null\`. \`status\` is still \`completed\`: check this field to learn whether the text was cut. |
| \`usage\` | \`null\` when the engine reported no counts. Never \`0\` for "not measured". |
| \`error\` | Present only on a failed response, carrying the same \`code\` vocabulary as the HTTP envelope. |

Read the text at
\`output[0].content[0].text\`, and write your client so an empty \`output\`
list is not a crash.
`;
const S_READ_ONE_BACK = `
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
`;
const S_CANCEL_ONE = `
## Cancel one

~~~http
POST /v1/responses/{id}/cancel
~~~

Requires \`responses.write\`, and is **idempotent** — cancelling an
already-finished or already-cancelled response is not an error, so a retry
after a network failure is safe. Like every call that presents a key, it is
recorded in the project's [usage](/docs/usage).

~~~bash
curl -X POST ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}/cancel \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~
`;
const S_SAFE_RETRIES = `
## Safe retries

Give any \`POST /v1/responses\` you might retry an
[\`Idempotency-Key\`](/docs/idempotency). Without one, a retry after a
timeout is a second generation: a second bill, and possibly a second answer
your user sees.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_CREATE_A_RESPONSE = `
## Create a response

~~~http
POST /v1/responses
~~~

Requires the \`responses.write\` scope. Three models generate here —
\`${MODEL_ID}\`, \`${VISION_MODEL_ID}\` and \`${OCR_MODEL_ID}\` — and one
scope covers all of them; which of them your key may use is your project's
model allowlist. The other models have endpoints of their own: see the
[model reference](/docs/models).

~~~json
{
  "model": "${MODEL_ID}",
  "input": "Explain retrieval-augmented generation.",
  "instructions": "Answer in British English.",
  "stream": true,
  "background": false,
  "store": true,
  "max_output_tokens": 1000,
  "temperature": 0.2,
  "metadata": { "customer_request_id": "abc-123" }
}
~~~
`;
const LATER_THE_FIELDS = `
### The fields

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. A chat model id your key may use. Otherwise \`404 model_not_found\`; a model of another kind, such as an embeddings model, is a \`400\` naming \`model\`. |
| \`input\` | string, or a list of messages | Required, non-empty. Over the model's input ceiling is \`400 context_length_exceeded\`. A \`user\` message may carry [images](/docs/images). |
| \`instructions\` | string | Optional. Must not be blank when present. |
| \`stream\` | boolean | Default \`false\`. See [streaming](/docs/streaming). |
| \`background\` | boolean | Default \`false\`. See [background responses](/docs/background). May be combined with \`stream\`. |
| \`store\` | boolean | Default \`true\`: while the request runs it is written down, so it survives a restart of the service and a broken stream can be [resumed](/docs/timeouts#resuming-a-stream). \`false\` keeps nothing, and the request is then cancelled when its client disconnects and cannot be resumed. |
| \`max_output_tokens\` | integer | Optional, at least 1, at most the model's ceiling — 1,000,000 on \`${MODEL_ID}\`. Clamped to what your prompt leaves in the window; see below. |
| \`temperature\` | number | Optional, \`0.0\` to \`2.0\`. Defaults to \`0.2\`, which is what the chat application asks for — except on \`${OCR_MODEL_ID}\`, where it defaults to \`0.0\`. |
| \`metadata\` | object of strings | Optional. At most 16 keys; keys ≤ 64 characters, values ≤ 512 characters; strings only. |

The whole body must be at most **20 MiB** — room for images — and all of the
*text* in it together at most **1 MiB**. The body size is checked before it is
parsed; either one over is \`413 request_too_large\`.

\`stream\` and \`background\` together give you a durable job you can watch as
it is written: the job carries on if your connection drops, and the stream can
be picked up again by id.
`;
const LATER_MAX_OUTPUT_TOKENS_PRECISELY = `
### \`max_output_tokens\`, precisely

* Send nothing: you get the platform default of **8,192**, clamped to the
  model's ceiling if that is lower.
* Send a value above the model's ceiling — 1,000,000 on \`${MODEL_ID}\`,
  24,576 on \`${VISION_MODEL_ID}\`, 8,192 on \`${OCR_MODEL_ID}\` — or above your
  project's own ceiling: a \`400\`. That value could never be honoured.
* Send a value within the ceiling that is more than your prompt leaves in the
  context window: it is **clamped** to the room that is left. Input and output
  share the window, so this is the only answer that is both honest and useful.
* Either way the response says what was applied, in its own
  \`max_output_tokens\` field, and \`incomplete_details\` says whether the
  answer stopped because it reached it. Nothing is clamped silently.

A long answer takes a long time — a million tokens is hours — and every mode
waits for it: there is no wall clock, and a synchronous call sends a byte every
15 seconds while it runs. Set your client's timeouts as
[timeouts](/docs/timeouts) describes. [Long outputs](/docs/long-output) has the
numbers.
`;
const LATER_THE_RESPONSE = `
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
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 }
}
~~~

| Field | Notes |
| --- | --- |
| \`id\` | \`resp_\` and 24 hex characters. Keep it: it is how you read, cancel, resume and correlate the response. |
| \`status\` | One of \`queued\`, \`in_progress\`, \`completed\`, \`failed\`, \`cancelled\`. |
| \`output\` | A list, always. One assistant message today; it stays a list so a future item kind does not change the shape. |
| \`max_output_tokens\` | The output ceiling **applied** to this generation, after any clamping. \`null\` only on a response created before 2026-09-13. |
| \`incomplete_details\` | \`{"reason": "max_output_tokens"}\` when the answer stopped because it reached that ceiling, otherwise \`null\`. \`status\` is still \`completed\`: check this field to learn whether the text was cut. |
| \`usage\` | \`null\` when the engine reported no counts. Never \`0\` for "not measured". |
| \`error\` | Present only on a failed response, carrying the same \`code\` vocabulary as the HTTP envelope. |

Read the text at
\`output[0].content[0].text\`, and write your client so an empty \`output\`
list is not a crash.

**A synchronous answer can be a \`200\` with \`"status": "failed"\`.** When a
synchronous request runs past 15 seconds, the \`200\` and a few spaces are sent
before the answer is known, so a failure after that is reported in the body.
Check \`status\` before you use \`output\`.
`;
const LATER_READ_ONE_BACK = `
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
\`output\` list: the text went to you once and was not stored.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

### Stream it back

~~~http
GET /v1/responses/{id}?stream=true&starting_after=412
~~~

Replays the response's events after \`starting_after\` (all of them without it),
then follows the generation live and closes after its terminal event. This is
how a broken stream is [resumed](/docs/timeouts#resuming-a-stream), and how a
background job is watched.

* Only the key that created the response, or another key of its service
  account, can do this; any other key gets \`404\`.
* Events are kept while the response runs and for one hour after it ends.
  After that, or for a response created with \`"store": false\`, the request is
  a \`400\` with \`param\` \`stream\`.
* \`starting_after\` without \`stream=true\` is a \`400\`.
`;
const LATER_SAFE_RETRIES = `
## Safe retries

Give any \`POST /v1/responses\` you might retry an
[\`Idempotency-Key\`](/docs/idempotency). A retry with the same key and body
joins the request if it is still running and replays it if it finished; without
a key, a retry of your own is a second generation — a second bill, and possibly
a second answer your user sees.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function responsesPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, LATER_CREATE_A_RESPONSE, LATER_THE_FIELDS, S_MESSAGES, S_A_PARAMETER_WE_CANNOT_HONOUR_IS_REJECTED, LATER_MAX_OUTPUT_TOKENS_PRECISELY, LATER_THE_RESPONSE, LATER_READ_ONE_BACK, S_CANCEL_ONE, LATER_SAFE_RETRIES]
    : [INTRO, S_CREATE_A_RESPONSE, S_THE_FIELDS, S_MESSAGES, S_A_PARAMETER_WE_CANNOT_HONOUR_IS_REJECTED, S_MAX_OUTPUT_TOKENS_PRECISELY, S_THE_RESPONSE, S_READ_ONE_BACK, S_CANCEL_ONE, S_SAFE_RETRIES];
  return {
    slug: 'responses',
    title: 'The Responses API',
    summary:
      'POST /v1/responses is the generation endpoint: synchronous, streaming ' +
      'or background, with every field validated rather than quietly ignored.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const responses: DocPage = responsesPage({ noTimeout: NO_TIMEOUT_LIVE });
