import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EMBED_MODEL_ID,
  EXAMPLE_STATUS,
  MODEL_ID,
  OCR_MODEL_ID,
  RERANK_MODEL_ID,
  VISION_MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';

// 2026-09-13, owner request: /v1 offers every model TechSara runs, not only
// techsara-35b. This page went from "one public model" to the six-model
// reference. Every ceiling below is the registry's default on the current
// deployment (CONTRACT §12.2); the page says, twice, to read them from
// GET /v1/models instead, because they follow the engines.
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): the embeddings, rerank and
// speech limits in the catalogue change with it — 2,048 inputs, 1,000
// documents, audio of any length up to 89 MiB a request — and a capacity queue
// no longer refuses. Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `

`;
const S_THE_SIX_MODELS_AT_A_GLANCE = `
## The six models at a glance

| Model | Kind | For | Endpoints |
| --- | --- | --- | --- |
| \`${MODEL_ID}\` | chat | The model that answers in the TechSara chat application: long answers, long documents, text and images. | [\`/v1/responses\`](/docs/responses), [\`/v1/chat/completions\`](/docs/chat-completions) |
| \`${VISION_MODEL_ID}\` | chat | A smaller model for short tasks over text and images. | [\`/v1/responses\`](/docs/responses), [\`/v1/chat/completions\`](/docs/chat-completions) |
| \`${OCR_MODEL_ID}\` | chat | Reads the text out of one image. | [\`/v1/responses\`](/docs/responses), [\`/v1/chat/completions\`](/docs/chat-completions) |
| \`${EMBED_MODEL_ID}\` | embedding | Vectors for search and clustering. | [\`/v1/embeddings\`](/docs/embeddings) |
| \`${RERANK_MODEL_ID}\` | rerank | Scores documents against a query. | [\`/v1/rerank\`](/docs/rerank) |
| \`${WHISPER_MODEL_ID}\` | transcription | Speech to text. | [\`/v1/audio/transcriptions\`](/docs/audio-transcriptions) |

Sending a model to an endpoint its kind does not serve is a \`400\` with
\`param\` \`model\` — for example "The model \`${EMBED_MODEL_ID}\` does not
support /v1/responses." — rather than a \`404\`: you are allowed to use the
model, just not there.
`;
const S_MODEL_ID = `
### ${MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Context window | 1,000,000 tokens, **shared by input and output** |
| Input | 999,232 tokens, and in practice bounded by the 1 MiB text rule — roughly 250,000 to 350,000 tokens of prose |
| Output | up to 1,000,000 tokens; 8,192 when you do not ask |
| Images | up to 16 per request, as \`data:\` URLs — see [images](/docs/images) |

The one model here that can write a book-length answer — and one that takes
hours to do it. Because input and output share the window, a large
\`max_output_tokens\` is **clamped** to what your prompt leaves, and the
response tells you the value it applied. Read
[long outputs](/docs/long-output) before asking for more than a few thousand
tokens. It answers directly, with no separate reasoning phase on this API.
`;
const S_VISION_MODEL_ID = `
### ${VISION_MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Context window | 24,576 tokens, shared by input and output |
| Output | up to 24,576 tokens; 8,192 when you do not ask |
| Images | up to 8 per request |

**Why the window is 24,576.** The engine behind this model also does work
for the TechSara chat application on every single turn. The public window is
set to about half of what that engine can hold, so that no public request —
however large — can take the room those turns need. It is a capacity decision
on this deployment, not a limit of the model, and like every ceiling it is
reported by the models endpoint, which is where a change would show first.
`;
const S_OCR_MODEL_ID = `
### ${OCR_MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Context window | 8,192 tokens, shared by the image and the text read out of it |
| Output | up to 8,192 tokens, clamped to what the image leaves — usually about 6,000 for a page |
| Images | **exactly one** per request; none, or two, is a \`400\` |
| Temperature | defaults to \`0.0\` |

**Leave the prompt out.** When your request carries no text and no
\`instructions\`, the server adds the plain instruction \`OCR\`, which is the
phrasing this model reads most accurately. Text you do send is passed through
exactly as you wrote it — and some phrasings, such as asking it to "parse the
document", make this model repeat itself until it runs out of output tokens.
If you write your own prompt, test it on your documents first.

The output clamp reserves a conservative 2,048 tokens per image, so a request
with a long prompt can see a lower applied \`max_output_tokens\` than you
might expect; the response always tells you the value it applied.
`;
const S_THE_CAPABILITY_FLAGS = `
## The capability flags

| Flag | What it means for your code |
| --- | --- |
| \`chat\` | The model takes a conversation of \`system\`, \`user\` and \`assistant\` turns on [\`/v1/responses\`](/docs/responses) and [\`/v1/chat/completions\`](/docs/chat-completions). |
| \`streaming\` | [Server-sent events](/docs/streaming) are supported on those two endpoints. |
| \`vision\` | The model accepts image parts in a \`user\` message — see [images](/docs/images). A request with an image to a model whose \`vision\` is \`false\` is a \`400\`. |
| \`ocr\` | The model is built to read text out of an image, one image per request. |
| \`background\` | \`"background": true\` is accepted — see [background responses](/docs/background). |
| \`embeddings\` | The model serves [\`/v1/embeddings\`](/docs/embeddings). |
| \`rerank\` | The model serves [\`/v1/rerank\`](/docs/rerank). |
| \`audio_transcription\` | The model serves [\`/v1/audio/transcriptions\`](/docs/audio-transcriptions). |
| \`tools\` | \`false\` for every model. Tool calling is not offered. See [tool calling](/docs/tools). |

\`endpoints\` lists the paths a model serves, and \`limits\` carries the
per-request limits that are not token counts. Write your client to ignore a
flag or a limit it does not recognise: new ones are additive.
`;
const S_WHICH_MODELS_EXIST_AND_WHO_DECIDES = `
## Which models exist, and who decides

The catalogue is declared in code and narrowed by configuration — never the
other way round. A deployment cannot publish a model by adding a container,
and an administrator can only take a declared model *away*. The engines
themselves are never reachable from outside: every model is reached through
\`/v1\`, with your key, and every call is recorded in your
[usage](/docs/usage).

**Not every deployment runs every model.** A model whose engine is not
configured on the deployment you are calling is simply not listed, and is
\`404 model_not_found\` everywhere, exactly like one your key may not use.
List the models first rather than assuming.

Practically: if it is not in \`GET /v1/models\`, it does not exist for you,
and the answer to "can you enable X for my key" is a review, not a toggle.
`;
const S_CHOOSING_A_MODEL_IN_A_REQUEST = `
## Choosing a model in a request

~~~json
{ "model": "${VISION_MODEL_ID}", "input": "…" }
~~~

The id must be one your key may use. Your project can carry a model
allowlist; if it is empty, every available public model is open to it —
including models added later. If that is not what you want, set an allowlist.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_CATALOGUE = `
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
      "kind": "chat",
      "capabilities": {
        "chat": true, "streaming": true, "vision": true, "tools": false,
        "embeddings": false, "rerank": false, "audio_transcription": false,
        "ocr": false, "background": true
      },
      "endpoints": ["/v1/responses", "/v1/chat/completions"],
      "context_window": 1000000,
      "max_input_tokens": 999232,
      "max_output_tokens": 1000000,
      "default_max_output_tokens": 8192,
      "limits": { "max_images_per_request": 16 }
    },
    {
      "id": "${VISION_MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "kind": "chat",
      "capabilities": {
        "chat": true, "streaming": true, "vision": true, "tools": false,
        "embeddings": false, "rerank": false, "audio_transcription": false,
        "ocr": false, "background": true
      },
      "endpoints": ["/v1/responses", "/v1/chat/completions"],
      "context_window": 24576,
      "max_input_tokens": 24320,
      "max_output_tokens": 24576,
      "default_max_output_tokens": 8192,
      "limits": { "max_images_per_request": 8 }
    },
    {
      "id": "${OCR_MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "kind": "chat",
      "capabilities": {
        "chat": true, "streaming": true, "vision": true, "tools": false,
        "embeddings": false, "rerank": false, "audio_transcription": false,
        "ocr": true, "background": true
      },
      "endpoints": ["/v1/responses", "/v1/chat/completions"],
      "context_window": 8192,
      "max_input_tokens": 7936,
      "max_output_tokens": 8192,
      "default_max_output_tokens": 8192,
      "limits": { "max_images_per_request": 1 }
    },
    {
      "id": "${EMBED_MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "kind": "embedding",
      "capabilities": {
        "chat": false, "streaming": false, "vision": false, "tools": false,
        "embeddings": true, "rerank": false, "audio_transcription": false,
        "ocr": false, "background": false
      },
      "endpoints": ["/v1/embeddings"],
      "context_window": 4096,
      "max_input_tokens": 4096,
      "max_output_tokens": null,
      "default_max_output_tokens": null,
      "limits": { "max_inputs_per_request": 2048, "embedding_dimensions": 1024 }
    },
    {
      "id": "${RERANK_MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "kind": "rerank",
      "capabilities": {
        "chat": false, "streaming": false, "vision": false, "tools": false,
        "embeddings": false, "rerank": true, "audio_transcription": false,
        "ocr": false, "background": false
      },
      "endpoints": ["/v1/rerank"],
      "context_window": 4096,
      "max_input_tokens": 4096,
      "max_output_tokens": null,
      "default_max_output_tokens": null,
      "limits": { "max_documents_per_request": 1000 }
    },
    {
      "id": "${WHISPER_MODEL_ID}",
      "object": "model",
      "owned_by": "techsara",
      "status": "available",
      "kind": "transcription",
      "capabilities": {
        "chat": false, "streaming": false, "vision": false, "tools": false,
        "embeddings": false, "rerank": false, "audio_transcription": true,
        "ocr": false, "background": false
      },
      "endpoints": ["/v1/audio/transcriptions"],
      "context_window": null,
      "max_input_tokens": null,
      "max_output_tokens": null,
      "default_max_output_tokens": null,
      "limits": {
        "max_audio_bytes": 93323264,
        "response_formats": ["json", "text", "verbose_json"]
      }
    }
  ]
}
~~~

Each object is exactly what the model registry renders, field for field,
inside the \`{"object": "list", "data": […]}\` envelope every listing on this
API uses. A number that does not apply to a model — an output ceiling on a
model that generates nothing — is \`null\`, never \`0\`.

The numbers above are an **illustration, not a promise**. They are read at
request time from what this deployment is actually running, and they move
when an engine is upgraded or reconfigured. Read them from this endpoint —
that is what it is for — rather than copying them into your client.

A single model resolves the same way:

~~~bash
curl ${API_BASE_URL}/models/${MODEL_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

A model you are not permitted to use answers \`404 model_not_found\`, not
\`403\`. The API does not confirm the existence of something you may not
reach.
`;
const LATER_EMBED_MODEL_ID = `
### ${EMBED_MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Dimensions | 1,024 |
| Inputs | up to 2,048 per request, in a body of at most 8 MiB |
| Input length | 4,096 tokens each — over it is a \`400\` naming the input, never a silent truncation |

Text is embedded exactly as you send it. For search, the model does best when
a **query** carries a one-line task description and documents do not — see
[embeddings](/docs/embeddings#queries-and-documents).
`;
const LATER_RERANK_MODEL_ID = `
### ${RERANK_MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Documents | up to 1,000 per request, in a body of at most 8 MiB |
| Pair length | 4,096 tokens for the query and one document together, after the server's template |

\`relevance_score\` is the model's probability, from 0 to 1, that the
document answers the query. See [rerank](/docs/rerank).
`;
const LATER_WHISPER_MODEL_ID = `
### ${WHISPER_MODEL_ID}

| Ceiling | Value |
| --- | --- |
| Audio length | no limit |
| File size | 89 MiB per request |
| Formats | \`json\`, \`text\`, \`verbose_json\` |

Let it detect the language. Forcing \`language\` forces the language of the
*output*: \`en\` on speech in another language gives you an English
translation rather than a transcript. Accuracy varies by language, and some —
Gujarati among them — are noticeably weaker and slower. See
[audio transcriptions](/docs/audio-transcriptions).
`;
const LATER_LIMITS_ATTACHED_TO_A_MODEL = `
## Limits attached to a model

* \`max_input_tokens\` — the prompt ceiling. Over it is
  \`400 context_length_exceeded\`, refused before the model runs rather than
  discovered halfway through generation.
* \`max_output_tokens\` — the ceiling for \`max_output_tokens\` in a request.
  Asking for more than the ceiling is a \`400\`. Asking for less, but more than
  your prompt leaves in the window, is **clamped**, and the response says what
  was applied. Asking for nothing gives you \`default_max_output_tokens\`.
* \`limits\` — images, inputs, documents or audio per request.

A project can set lower input and output ceilings of its own, which then
apply to every key in it. These are the limits that apply: the API enforces no
per-project request, token or concurrency limits on top of them — see
[rate limits](/docs/rate-limits). What it does have is a capacity queue in
front of each shared engine, where a request waits its turn for as long as it
takes and is never refused — see
[rate limits](/docs/rate-limits#capacity-queues-per-engine).
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function modelsPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, LATER_THE_CATALOGUE, S_THE_SIX_MODELS_AT_A_GLANCE, S_MODEL_ID, S_VISION_MODEL_ID, S_OCR_MODEL_ID, LATER_EMBED_MODEL_ID, LATER_RERANK_MODEL_ID, LATER_WHISPER_MODEL_ID, S_THE_CAPABILITY_FLAGS, S_WHICH_MODELS_EXIST_AND_WHO_DECIDES, S_CHOOSING_A_MODEL_IN_A_REQUEST, LATER_LIMITS_ATTACHED_TO_A_MODEL]
    // The sidecar facts (2,048 inputs, 1,000 documents, audio of any length,
    // capacity queues that wait) shipped before the rest of the no-timeout
    // release (2026-09-14), and the catalogue sample must match what the
    // registry reports today, so today's page already uses those sections.
    : [INTRO, LATER_THE_CATALOGUE, S_THE_SIX_MODELS_AT_A_GLANCE, S_MODEL_ID, S_VISION_MODEL_ID, S_OCR_MODEL_ID, LATER_EMBED_MODEL_ID, LATER_RERANK_MODEL_ID, LATER_WHISPER_MODEL_ID, S_THE_CAPABILITY_FLAGS, S_WHICH_MODELS_EXIST_AND_WHO_DECIDES, S_CHOOSING_A_MODEL_IN_A_REQUEST, LATER_LIMITS_ATTACHED_TO_A_MODEL];
  return {
    slug: 'models',
    title: 'Model reference',
    summary:
      'Six public models — chat, vision, OCR, embeddings, reranking and speech ' +
      'to text — what each accepts, and why you should read its limits from the API.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const models: DocPage = modelsPage({ noTimeout: NO_TIMEOUT_LIVE });
