import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const models: DocPage = {
  slug: 'models',
  title: 'Model reference',
  summary:
    'One public model, its capabilities, and why you should read its limits ' +
    'from the API rather than from this page.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
## The catalogue

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/models \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

~~~json
{
  "object": "list",
  "data": [
    {
      "id": "${MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "capabilities": {
        "chat": true,
        "streaming": true,
        "vision": true,
        "tools": false,
        "embeddings": false
      },
      "max_input_tokens": 1000000,
      "max_output_tokens": 8192
    }
  ]
}
~~~

The per-model object is exactly what the model registry renders, field for
field, inside the \`{"object": "list", "data": […]}\` envelope every listing
on this API uses.

The two token numbers above are an **illustration, not a promise**. They are
read at request time from the engine this deployment is actually running, and
they move when it is upgraded. Read them from this endpoint — that is what it
is for — rather than copying them into your client.

A single model resolves the same way:

~~~bash
curl ${API_BASE_URL}/models/${MODEL_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

A model you are not permitted to use answers \`404 model_not_found\`, not
\`403\`. The API does not confirm the existence of something you may not
reach.

## The capability flags, honestly

| Flag | Today | What it means for your code |
| --- | --- | --- |
| \`chat\` | \`true\` | The model takes a conversation of \`system\`, \`user\` and \`assistant\` turns. |
| \`streaming\` | \`true\` | [Server-sent events](/docs/streaming) are supported on \`/v1/responses\` and \`/v1/chat/completions\`. |
| \`vision\` | model-dependent | The underlying model is a vision-language model, **but \`/v1\` accepts text input only today** — see below. |
| \`tools\` | \`false\` | Tool calling is not offered. See [tool calling](/docs/tools). |
| \`embeddings\` | \`false\` | There is no embeddings endpoint on this platform. |

### Vision is a model capability, not an API capability

The flag reports what the model can do. The API is narrower: a message's
\`content\` is a string, and the multimodal part list some clients send is
rejected rather than accepted and dropped. That is the safer of the two
failures — a caller whose image parts were silently discarded would be
charged for a prompt the model never saw, and would have no way to tell.

If image input matters to you, say so; it is a contract change, not a
configuration one.

## Which models exist, and who decides

The catalogue is declared in code and narrowed by configuration — never the
other way round. A deployment cannot publish a model by adding a container,
and an administrator can only take a declared model *away*. The router, the
embeddings service, the OCR engine and the reranker are not models on this
platform, have no entry in the registry, and have no reachable path from
\`/v1\` at all.

Practically, that means: if it is not in \`GET /v1/models\`, it does not
exist for you, and the answer to "can you enable X for my key" is a review,
not a toggle.

## Choosing a model in a request

~~~json
{ "model": "${MODEL_ID}", "input": "…" }
~~~

The id must be one your key may use. Your project can carry a model
allowlist; if it is empty, every public model is available to it.

## Limits attached to a model

* \`max_input_tokens\` — the prompt ceiling. Over it is
  \`400 context_length_exceeded\`, refused before admission rather than
  discovered halfway through generation.
* \`max_output_tokens\` — the ceiling for \`max_output_tokens\` in a request.
  Asking for more than the model can give is a \`400\`; asking for nothing at
  all gives you the platform default, clamped to the ceiling.

A project can set lower input and output ceilings of its own, which then
apply to every key in it.
See [rate limits](/docs/rate-limits) for the per-project limits that sit on
top of these.
`.trim(),
};
