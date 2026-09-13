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
    'Every model TechSara runs — chat, vision, OCR, embeddings, reranking and ' +
    'speech to text — behind one API key, served from the same engines as the ' +
    'TechSara chat application.',
  section: 'Getting started',
  examples: EXAMPLE_STATUS,
  body: `
The TechSara developer platform lets your own software use the models that
power TechSara AI: send prompts and images to the chat models and read the
answer back — synchronously, as a stream, or as a background job that calls a
webhook when it finishes — and turn text into embeddings, rank documents
against a query, read text out of images and transcribe speech.

It is a small API on purpose. Six models, one generation endpoint, a
compatibility endpoint for clients you already have, one endpoint each for
embeddings, reranking and transcription, and the reading surfaces you need to
run them in production.

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
| POST | \`/v1/chat/completions\` | The compatibility shape, same models | \`responses.write\` |
| POST | \`/v1/embeddings\` | Vectors for search and clustering | \`embeddings.write\` |
| POST | \`/v1/rerank\` | Score documents against a query | \`rerank.write\` |
| POST | \`/v1/audio/transcriptions\` | Speech to text | \`audio.write\` |
| GET | \`/v1/usage\` | Your project's usage counters | \`usage.read\` |
| GET | \`/v1/openapi.json\` | The machine-readable schema | none |

[The model reference](/docs/models) says which model serves which endpoint.
[The Responses API](/docs/responses) is the one to build generation against.
[Chat Completions](/docs/chat-completions) exists so an existing client
library can point at this platform with a base-URL change.

## What is deliberately not here

The API runs models. It does not reach the rest of the product: there is no
endpoint for Salesforce data, retrieval or web search, deep research, file
uploads, artifacts, conversation history, or anyone's saved memory — and no tool
calling. Each of
those is a separate product decision with its own threat model, and none of
them is one flag away from being exposed.

Two consequences worth knowing before you design against it:

* **The API is stateless.** It never reads a person's chat memory and never
  writes to anyone's conversation history. If you want a conversation, send
  the turns you want the model to see in \`input\`.
* **Your prompts, images, audio and the generated text are not stored by default.** Request
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

The model most examples use is \`${MODEL_ID}\`, the one that answers in the chat
application — it can write up to a million tokens in one response (see
[long outputs](/docs/long-output)). Every model's real ceilings are reported by
[the models endpoint](/docs/models) — read them from there rather than copying
a number out of a document, because they follow the engines this deployment is
running.
`.trim(),
};
