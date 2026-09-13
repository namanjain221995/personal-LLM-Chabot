import type { DocPage } from '../types';
import { API_BASE_URL, CONSOLE_PATH, MODEL_ID, EXAMPLE_STATUS } from '../samples';

/**
 * The landing page at /docs. It answers the three questions a developer
 * arrives with — what is this, where do I send a request, what is it allowed
 * to do — and then gets out of the way.
 */
export const overview: DocPage = {
  slug: 'overview',
  title: 'TechSara developer platform',
  summary:
    'One text-generation API, authenticated with an API key, served from the ' +
    'same models that answer in the TechSara chat application.',
  section: 'Getting started',
  examples: EXAMPLE_STATUS,
  body: `
The TechSara developer platform lets your own software send prompts to the
model that powers TechSara AI and read the answer back — synchronously, as a
stream, or as a background job that calls a webhook when it finishes.

It is a small API on purpose. One model, one generation endpoint, a
compatibility endpoint for clients you already have, and the reading surfaces
you need to run them in production.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/models \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

## What you can call

| Method | Path | What it does | Scope |
| --- | --- | --- | --- |
| GET | \`/v1/models\` | The models this key may use | \`models.read\` |
| GET | \`/v1/models/{model}\` | One model's capabilities and limits | \`models.read\` |
| POST | \`/v1/responses\` | Generate — sync, streaming or background | \`responses.write\` |
| GET | \`/v1/responses/{id}\` | Read a response your project created | \`responses.read\` |
| POST | \`/v1/responses/{id}/cancel\` | Stop one, idempotently | \`responses.write\` |
| POST | \`/v1/chat/completions\` | The compatibility shape, same engine | \`responses.write\` |
| GET | \`/v1/usage\` | Your project's usage counters | \`usage.read\` |
| GET | \`/v1/openapi.json\` | The machine-readable schema | none |

[The Responses API](/docs/responses) is the one to build against.
[Chat Completions](/docs/chat-completions) exists so an existing client
library can point at this platform with a base-URL change.

## What is deliberately not here

The API generates text. It does not reach the rest of the product: there is no
endpoint for Salesforce data, retrieval or web search, deep research, file
uploads, artifacts, conversation history, or anyone's saved memory. Each of
those is a separate product decision with its own threat model, and none of
them is one flag away from being exposed.

Two consequences worth knowing before you design against it:

* **The API is stateless.** It never reads a person's chat memory and never
  writes to anyone's conversation history. If you want a conversation, send
  the turns you want the model to see in \`input\`.
* **Your prompts and the generated text are not stored by default.** Request
  logs keep metadata only — request id, time, project, model, status, token
  counts, duration, error code. The exception is a
  [background response](/docs/background), whose output is kept only so you
  can fetch it, and only for your project's retention window.

## Three surfaces, three credentials

| Surface | Credential |
| --- | --- |
| The chat application | Your browser session |
| The developer console at \`${CONSOLE_PATH}\` | Your browser session, plus the console capability |
| This API, under \`/v1\` | \`Authorization: Bearer <api key>\`, and nothing else |

\`/v1\` reads exactly one credential: the \`Authorization\` header. It ignores
cookies entirely, which is what stops a page on the internet from driving the
API with a signed-in person's session.

## Start here

1. [Quickstart](/docs/quickstart) — a key and a first answer.
2. [Authentication](/docs/authentication) — keys, scopes, environments.
3. [API-key security](/docs/key-security) — how keys are built, stored and rotated.
4. [Errors](/docs/errors) and [rate limits](/docs/rate-limits) — what production will actually hand you (no usage limits; a \`503\` during a model restart).

The model id you will use everywhere is \`${MODEL_ID}\`. Its real context and
output ceilings are reported by [the models endpoint](/docs/models) — read
them from there rather than copying a number out of a document, because they
follow the engine this deployment is running.
`.trim(),
};
