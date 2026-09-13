import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_STATUS,
  MODEL_ID,
  OCR_MODEL_ID,
  VISION_MODEL_ID,
} from '../samples';

// 2026-09-13, owner request (every model TechSara runs on /v1): image input
// arrived with techsara-8b-vision and techsara-ocr, and techsara-35b — which
// was always a vision-language model — accepts images through the API too.
// The page used to be one paragraph on the models page saying "text only".
export const images: DocPage = {
  slug: 'images',
  title: 'Images and OCR',
  summary:
    'Send images to the vision models and read text out of a page with ' +
    'techsara-ocr — as data: URLs, never links.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
Three models accept images on [\`/v1/responses\`](/docs/responses) and
[\`/v1/chat/completions\`](/docs/chat-completions):

| Model | Images per request | Use it for |
| --- | --- | --- |
| \`${MODEL_ID}\` | up to 16 | Reasoning over images together with long text. |
| \`${VISION_MODEL_ID}\` | up to 8 | Short questions about a screenshot, a chart or a photo. |
| \`${OCR_MODEL_ID}\` | exactly 1 | Reading the text out of one page or image. |

A model's \`capabilities.vision\` in [\`GET /v1/models\`](/docs/models) is
the authority; an image sent to a model whose \`vision\` is \`false\` is a
\`400\`.

## Sending an image

An image is a content part inside a \`user\` message, and its \`image_url\`
is a \`data:\` URL carrying the file itself:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

IMAGE_B64=$(base64 < slide.png | tr -d '\\n')

cat > request.json <<JSON
{
  "model": "${VISION_MODEL_ID}",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_text", "text": "What is the main point of this slide?" },
        { "type": "input_image", "image_url": "data:image/png;base64,\${IMAGE_B64}" }
      ]
    }
  ]
}
JSON

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  --data-binary @request.json
~~~

The body is written to a file first because a base64 image is far longer
than a shell argument should be. The response is the ordinary
[response object](/docs/responses#the-response).

The same request from Python:

~~~python
import base64
import os
import httpx

with open("slide.png", "rb") as fh:
    data_url = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")

response = httpx.post(
    "${API_BASE_URL}/responses",
    headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
    json={
        "model": "${VISION_MODEL_ID}",
        "input": [{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is the main point of this slide?"},
                {"type": "input_image", "image_url": data_url},
            ],
        }],
    },
    timeout=120.0,
)
response.raise_for_status()
print(response.json()["output"][0]["content"][0]["text"])
~~~

## The content parts

On \`/v1/responses\`, a \`user\` message's \`content\` is a string or a list
of these:

| Part | Fields |
| --- | --- |
| \`input_text\` | \`text\` — a string. |
| \`input_image\` | \`image_url\` — a \`data:\` URL; \`detail\` — optional, and only \`"auto"\`. |

On \`/v1/chat/completions\` the same thing is spelled the way that shape
spells it:

| Part | Fields |
| --- | --- |
| \`text\` | \`text\` — a string. |
| \`image_url\` | \`image_url\` — an object: \`url\`, a \`data:\` URL, and optional \`detail\`, only \`"auto"\`. |

~~~json
{
  "model": "${VISION_MODEL_ID}",
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "What is the main point of this slide?" },
        { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBORw0KGgo…" } }
      ]
    }
  ]
}
~~~

\`system\` and \`assistant\` messages stay strings: images go in \`user\`
turns only.

## The rules

| Rule | Over it, or outside it |
| --- | --- |
| \`image_url\` is a \`data:\` URL — \`https://\`, \`http://\`, \`file:\` and every other scheme are refused | \`400 invalid_request_error\` |
| The type is \`image/png\`, \`image/jpeg\`, \`image/webp\` or \`image/gif\`, base64-encoded | \`400\` |
| The bytes are what the type says — a JPEG labelled \`image/png\` is refused | \`400\` |
| Each image is at most **10 MiB** once decoded | \`400\` |
| No more images than the model's \`limits.max_images_per_request\` | \`400\` |
| The whole body is at most **20 MiB** on these two endpoints | \`413 request_too_large\` |
| All the *text* in the body together is still at most **1 MiB** | \`413 request_too_large\` |

**Why no links.** An image URL would be fetched by our servers, from inside
our network, on your behalf. That is the classic way to turn an API into a
probe of somebody else's private addresses, so the API never fetches a URL you
send: it accepts the image itself or nothing. Download the image on your side
and send the bytes.

**Budget for base64.** Encoding makes a file about a third larger, so one
10 MiB image is roughly 13.4 MiB of body. A 20 MiB body holds one image at the
limit, or several smaller ones. If you are near the limits, resize before you
encode: the models do not need a 40-megapixel photograph to read a slide.

An image costs input tokens — typically several hundred for an ordinary
screenshot, more for a large image — and they are in \`usage.input_tokens\`
with everything else. Images are never stored.

## Reading text with ${OCR_MODEL_ID}

Send exactly one image and **no text at all**:

~~~json
{
  "model": "${OCR_MODEL_ID}",
  "input": [
    {
      "role": "user",
      "content": [
        { "type": "input_image", "image_url": "data:image/jpeg;base64,/9j/4AAQSkZJRg…" }
      ]
    }
  ]
}
~~~

When a request to \`${OCR_MODEL_ID}\` carries no text part and no
\`instructions\`, the server adds the one-word instruction \`OCR\`. That is not
a default we picked for tidiness: it is the phrasing this model reads most
accurately and fastest. Anything you do write is passed through unaltered,
and some phrasings — asking it to parse or describe the document, for
instance — can make the model repeat itself until the output ceiling stops it.
Test a custom prompt on your own documents before you rely on it.

Two more things specific to this model:

* **Temperature defaults to \`0.0\`.** Reading is not a creative task.
* **The window is small and the image is in it.** The context window is 8,192
  tokens, shared by the image and the text read out of it. The server reserves
  a conservative 2,048 tokens per image before it sets the output ceiling, so a
  page usually leaves around 6,000 tokens for text, and the response's
  \`max_output_tokens\` tells you exactly what it applied. A dense page that
  needs more is better cut in two.

One image per request means a multi-page document is one request per page.
Send them in parallel, a few at a time — see the capacity note below — rather
than as one request with many images, which is a \`400\`.

## Capacity

\`${VISION_MODEL_ID}\` and \`${OCR_MODEL_ID}\` run on engines the TechSara chat
application also uses, and the chat application keeps priority. Public
requests to each engine share a small queue, and \`${OCR_MODEL_ID}\` briefly
steps aside while someone is chatting. When a queue does not free a place
within about 30 seconds, the request is \`503 model_unavailable\` with
\`Retry-After\` — the engine's capacity, the same for every caller, never a
limit on your key. Retry after the interval, with jitter. See
[rate limits](/docs/rate-limits#capacity-queues-per-engine).
`.trim(),
};
