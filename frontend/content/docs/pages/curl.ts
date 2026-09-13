import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';

export const curl: DocPage = {
  slug: 'curl',
  title: 'cURL',
  summary: 'Every endpoint as a one-liner, for a terminal and for a bug report.',
  section: 'Examples',
  examples: EXAMPLE_STATUS,
  body: `
~~~bash
export TECHSARA_API_KEY="tsk_live_…"
export TECHSARA_BASE_URL="${API_BASE_URL}"
~~~

## Models

~~~bash
curl "$TECHSARA_BASE_URL/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"

curl "$TECHSARA_BASE_URL/models/${MODEL_ID}" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

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

## Stream

~~~bash
curl -N "$TECHSARA_BASE_URL/responses" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${MODEL_ID}", "input": "Explain RAG.", "stream": true}'
~~~

\`-N\` turns curl's buffering off. Without it the whole stream arrives at
once and streaming looks broken.

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

## Cancel

~~~bash
curl -X POST "$TECHSARA_BASE_URL/responses/${EXAMPLE_RESPONSE_ID}/cancel" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

## Usage

~~~bash
curl "$TECHSARA_BASE_URL/usage" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

## The schema

~~~bash
curl "$TECHSARA_BASE_URL/openapi.json"
~~~

No key required, and it describes only the public surface.

## Reading the headers

~~~bash
curl -i "$TECHSARA_BASE_URL/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`-i\` prints \`X-Request-Id\` (log it) and, on a \`503\`,
\`Retry-After\`. There is no \`RateLimit\` header to read: the API enforces
no usage limits (see [rate limits](/docs/rate-limits)).

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
`.trim(),
};
