import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EMBED_MODEL_ID,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
  OCR_MODEL_ID,
  RERANK_MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): `curl -N` and no `--max-time`
// for anything long, and the resume request. Built in either state from
// NO_TIMEOUT_LIVE.

const INTRO = `
~~~bash
export TECHSARA_API_KEY="tsk_live_…"
export TECHSARA_BASE_URL="${API_BASE_URL}"
~~~
`;
const S_MODELS = `
## Models

~~~bash
curl "$TECHSARA_BASE_URL/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"

curl "$TECHSARA_BASE_URL/models/${MODEL_ID}" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~
`;
const S_GENERATE = `
## Generate

~~~bash
curl "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Explain retrieval-augmented generation in two sentences."
  }'
~~~

With a conversation, an instruction and the sampling knobs this platform
accepts:

~~~bash
curl "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "instructions": "Answer in British English.",
    "input": [
      {"role": "system", "content": "You are a concise technical writer."},
      {"role": "user", "content": "Summarise our uptime policy."}
    ],
    "temperature": 0.2,
    "max_output_tokens": 600,
    "metadata": {"customer_request_id": "abc-123"}
  }'
~~~
`;
const S_STREAM = `
## Stream

~~~bash
curl -N "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${MODEL_ID}", "input": "Explain RAG.", "stream": true}'
~~~

\`-N\` turns curl's buffering off. Without it the whole stream arrives at
once and streaming looks broken.
`;
const S_BACKGROUND_THEN_COLLECT = `
## Background, then collect

~~~bash
curl "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -d '{"model": "${MODEL_ID}", "input": "Summarise this policy.", "background": true}'

curl "$TECHSARA_BASE_URL/responses/${EXAMPLE_RESPONSE_ID}" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~
`;
const S_CANCEL = `
## Cancel

~~~bash
curl -X POST "$TECHSARA_BASE_URL/responses/${EXAMPLE_RESPONSE_ID}/cancel" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~
`;
const S_A_LONG_ANSWER = `
## A long answer

~~~bash
curl "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -d '{"model": "${MODEL_ID}", "input": "Write the full handbook.", "max_output_tokens": 1000000, "background": true}'
~~~

The \`202\` carries the planned \`max_output_tokens\`, already clamped to the
window. A million tokens takes hours — see [long outputs](/docs/long-output).
`;
const S_READ_TEXT_OUT_OF_AN_IMAGE = `
## Read text out of an image

~~~bash
printf '{"model": "${OCR_MODEL_ID}", "input": [{"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,%s"}]}]}' \\
  "$(base64 < page.png | tr -d '\\n')" > ocr.json

curl "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  --data-binary @ocr.json
~~~

No text part: the server adds the instruction this model reads best with. See
[images and OCR](/docs/images).
`;
const S_EMBEDDINGS = `
## Embeddings

~~~bash
curl "$TECHSARA_BASE_URL/embeddings" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${EMBED_MODEL_ID}", "input": ["first passage", "second passage"]}'
~~~
`;
const S_RERANK = `
## Rerank

~~~bash
curl "$TECHSARA_BASE_URL/rerank" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${RERANK_MODEL_ID}", "query": "How do I rotate a key?", "documents": ["Keys rotate in the console.", "Webhooks are signed."], "top_n": 1}'
~~~
`;
const S_TRANSCRIBE_AUDIO = `
## Transcribe audio

~~~bash
curl "$TECHSARA_BASE_URL/audio/transcriptions" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -F "file=@standup.m4a;type=audio/mp4" \\
  -F "model=${WHISPER_MODEL_ID}" \\
  -F "response_format=verbose_json"
~~~

Name the file's type with \`;type=\`; an untyped part is refused.
`;
const S_USAGE = `
## Usage

~~~bash
curl "$TECHSARA_BASE_URL/usage" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~
`;
const S_THE_SCHEMA = `
## The schema

~~~bash
curl "$TECHSARA_BASE_URL/openapi.json"
~~~

No key required, and it describes only the public surface.
`;
const S_READING_THE_HEADERS = `
## Reading the headers

~~~bash
curl -i "$TECHSARA_BASE_URL/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`-i\` prints \`X-Request-Id\` (log it) and, on a \`503\`,
\`Retry-After\`. There is no \`RateLimit\` header to read: the API enforces
no usage limits (see [rate limits](/docs/rate-limits)).
`;
const S_WHEN_SOMETHING_IS_WRONG = `
## When something is wrong

~~~bash
curl -sS -i "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${MODEL_ID}", "input": "hello", "top_p": 0.9}'
~~~

\`top_p\` is not a parameter this platform can honour, so it is refused with
\`400\` and \`"param": "top_p"\` rather than accepted and ignored. That is
the general rule — see [the Responses API](/docs/responses#a-parameter-we-cannot-honour-is-rejected).

Send the \`X-Request-Id\` with any report. Never send the key.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_A_LONG_ANSWER = `
## A long answer

~~~bash
curl -N "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: $(uuidgen)" \\
  -d '{"model": "${MODEL_ID}", "input": "Write the full handbook.", "max_output_tokens": 1000000, "stream": true}'
~~~

No \`--max-time\`: it limits the whole transfer, and a million tokens takes
hours. The API sends a byte at least every 15 seconds, so nothing on the way
gives up. \`"background": true\` instead of \`"stream": true\` hands the job
over and returns a \`202\` at once — see [long outputs](/docs/long-output).

## Resume a stream

~~~bash
curl -N "$TECHSARA_BASE_URL/responses/${EXAMPLE_RESPONSE_ID}?stream=true&starting_after=412" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

Every event after sequence number 412, then the rest live. Only the key that
created the response can replay it. See [timeouts](/docs/timeouts#resuming-a-stream).
`;
const LATER_READING_THE_HEADERS = `
## Reading the headers

~~~bash
curl -i "$TECHSARA_BASE_URL/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`-i\` prints \`X-Request-Id\` (log it), and on a failure \`Retry-After\` and
\`x-should-retry\`. There is no \`RateLimit\` header to read: the API enforces
no usage limits (see [rate limits](/docs/rate-limits)).
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function curlPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, S_MODELS, S_GENERATE, S_STREAM, S_BACKGROUND_THEN_COLLECT, S_CANCEL, LATER_A_LONG_ANSWER, S_READ_TEXT_OUT_OF_AN_IMAGE, S_EMBEDDINGS, S_RERANK, S_TRANSCRIBE_AUDIO, S_USAGE, S_THE_SCHEMA, LATER_READING_THE_HEADERS, S_WHEN_SOMETHING_IS_WRONG]
    : [INTRO, S_MODELS, S_GENERATE, S_STREAM, S_BACKGROUND_THEN_COLLECT, S_CANCEL, S_A_LONG_ANSWER, S_READ_TEXT_OUT_OF_AN_IMAGE, S_EMBEDDINGS, S_RERANK, S_TRANSCRIBE_AUDIO, S_USAGE, S_THE_SCHEMA, S_READING_THE_HEADERS, S_WHEN_SOMETHING_IS_WRONG];
  return {
    slug: 'curl',
    title: 'cURL',
    summary: 'Every endpoint as a one-liner, for a terminal and for a bug report.',
    section: 'Examples',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const curl: DocPage = curlPage({ noTimeout: NO_TIMEOUT_LIVE });
