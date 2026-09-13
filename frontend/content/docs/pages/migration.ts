import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const migration: DocPage = {
  slug: 'migration',
  title: 'Migration and compatibility',
  summary:
    'Porting an existing OpenAI-shaped client, and what this platform ' +
    'promises about changing under you.',
  section: 'Reference',
  examples: EXAMPLE_STATUS,
  body: `
## Porting an existing client

\`POST /v1/chat/completions\` accepts the widely used chat-completions
request shape, so an existing client usually needs three changes:

1. **Base URL** → \`${API_BASE_URL}\`
2. **API key** → a TechSara key (\`tsk_live_…\` or \`tsk_test_…\`)
3. **Model** → \`${MODEL_ID}\`

Then run it and read the \`400\`s. They are the list of things to remove.

## The differences, in one table

| What you may be doing | Here |
| --- | --- |
| Sending \`top_p\`, \`n\`, \`seed\`, \`logit_bias\`, \`presence_penalty\`… | Rejected with \`400\` and the field in \`param\`. Remove them. |
| Sending \`tools\` / \`functions\` | Rejected. [Tool calling](/docs/tools) is not offered. |
| Sending image or audio parts in \`content\` | Rejected. \`content\` is a string. |
| Expecting several models | One public model; a model your key may not use is \`404\`. |
| Expecting \`usage\` to be \`0\` when unmeasured | It is \`null\`. Never treat \`null\` as zero. |
| Relying on assistants, threads, files or embeddings | None of those exist on this platform. |
| Parsing a vendor-specific error body | The envelope is \`{"error": {message, type, code, param, request_id}}\`. See [errors](/docs/errors). |

Everything else — \`messages\`, \`stream\`, \`temperature\`, the
\`chat.completion.chunk\` streaming frames and the final \`data: [DONE]\` —
works the way the shape says it does. See
[Chat Completions compatibility](/docs/chat-completions).

## Why we reject instead of ignore

It is the rule the whole API is built on: a parameter this platform cannot
honour is refused, never silently dropped. Porting is therefore noisier here
than you may be used to, and the noise is the feature — you finish the port
knowing exactly which knobs are live, instead of shipping a client that
believes it is setting \`top_p\`.

## Moving to the Responses API

Build new work against [the Responses API](/docs/responses); compatibility is
for code you already have. The move is small:

| Chat Completions | Responses |
| --- | --- |
| \`messages: [{role, content}]\` | \`input\`: the same list, or a plain string for one user turn |
| a system message | \`instructions\`, which becomes the first system message |
| \`max_tokens\` | \`max_output_tokens\` |
| \`choices[0].message.content\` | \`output[0].content[0].text\` |
| \`data:\` chunks, then \`[DONE]\` | named lifecycle events, ending at \`response.completed\` |
| — | \`background: true\`, with a webhook when it finishes |

The Responses shape is where background work, the numbered event stream and
anything this platform adds later will live.

## What we promise about change

* **The version is in the path.** \`/v1\` is the contract you are coding
  against.
* **Additive changes can happen inside \`/v1\`** — a new optional field, a new
  event name, a new model id. Write clients that ignore fields and events they
  do not recognise, and none of those will reach you.
* **Behaviour is defined in one place.** Every route, field, error code and
  event on this surface comes from the developer-platform contract in this
  repository; changing behaviour means changing that document first, in a
  reviewed diff. There is no configuration switch that quietly changes what
  \`/v1\` does.
* **The schema is machine-readable.** \`GET /v1/openapi.json\` describes the
  public surface and nothing else. Generate from it; diff it between releases.
* **Removals are announced** in the [changelog](/docs/changelog).

## A porting checklist

1. Point a **test** key at a test project.
2. Send your existing request. Collect the \`400\`s and delete those fields.
3. Replace your error handling with the [error codes](/docs/errors), and keep
   \`request_id\`.
4. Add an [\`Idempotency-Key\`](/docs/idempotency) to anything you retry.
5. Read the \`RateLimit\` headers and size your concurrency to the project's
   limits — see [rate limits](/docs/rate-limits).
6. Move long work to [background responses](/docs/background) before you meet
   your first proxy timeout, not after.
`.trim(),
};
