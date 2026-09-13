import type { DocPage } from '../types';
import { API_BASE_URL, EXAMPLE_STATUS, RERANK_MODEL_ID } from '../samples';

// 2026-09-13, owner request: the reranker is on /v1. The request shape is the
// common query-plus-documents rerank shape; the facts a caller cannot guess —
// what the score means, that nothing is truncated, that ties are returned as
// ties — are stated here (CONTRACT §8.5).
export const rerank: DocPage = {
  slug: 'rerank',
  title: 'Rerank',
  summary:
    'POST /v1/rerank scores up to 100 documents against a query with ' +
    'techsara-rerank and returns them best first.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
~~~http
POST /v1/rerank
~~~

Requires the \`rerank.write\` scope. Keys created before 2026-09-13 do not have
it — see [authentication](/docs/authentication#scopes).

A reranker reads a query and one document *together* and judges how well the
document answers it. That is slower than comparing vectors and much more
accurate, so the usual pattern is: fetch fifty or a hundred candidates cheaply
— by keyword or with [embeddings](/docs/embeddings) — then rerank them and keep
the best few.

## Rerank documents

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/rerank \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${RERANK_MODEL_ID}",
    "query": "How do I rotate an API key?",
    "documents": [
      "Webhooks are signed with HMAC-SHA256.",
      "Rotation mints a replacement and keeps the old key working for an overlap window.",
      {"text": "Usage is recorded once per request."}
    ],
    "top_n": 2,
    "return_documents": true
  }'
~~~

## The request

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. \`${RERANK_MODEL_ID}\`. |
| \`query\` | string | Required, non-empty. |
| \`documents\` | list | Required. 1 to 100 items, each a string or an object \`{"text": "…"}\`. |
| \`top_n\` | integer | Optional, at least 1. Return only the best \`top_n\`; all of them by default. |
| \`return_documents\` | boolean | Optional, default \`false\`. Echo each document's text back in its result. |
| \`instruction\` | string | Optional, at most 512 characters. One line telling the model what "relevant" means for your task. |

Any other field is a \`400\` naming it. The body is at most 1 MiB.

**Nothing is truncated.** The server wraps each query-and-document pair in the
model's own prompt format, and the pair must fit in **4,096 tokens**. A document
that does not fit is refused with \`400 context_length_exceeded\` and \`param\`
naming it — \`documents.7\` for the eighth — because a score for the first half
of a document is a confident answer about text the model never read. Split long
documents into passages and rerank the passages.

## The response

~~~json
{
  "id": "rrk_9c1e4b7a2d5f8e3c6b0a4d17",
  "object": "rerank",
  "model": "${RERANK_MODEL_ID}",
  "results": [
    {
      "index": 1,
      "relevance_score": 0.94,
      "document": { "text": "Rotation mints a replacement and keeps the old key working for an overlap window." }
    },
    {
      "index": 2,
      "relevance_score": 0.03,
      "document": { "text": "Usage is recorded once per request." }
    }
  ],
  "usage": { "input_tokens": 212, "total_tokens": 212 }
}
~~~

* \`results\` is sorted by \`relevance_score\`, highest first; equal scores
  keep the order you sent them in. It is cut to \`top_n\`.
* \`index\` is the document's position in your \`documents\` list.
* \`document\` is present only with \`"return_documents": true\`.
* \`usage\` is \`null\` — never \`0\` — when the engine did not report counts.

## What the score means

\`relevance_score\` is the model's **probability, between 0 and 1, that the
document answers the query**. It is comparable across requests with the same
instruction, so a fixed threshold is reasonable — start around \`0.5\` and tune
on your own data. It is not a similarity: two documents can both score near
\`0.0\` or both near \`1.0\`.

Equal scores are returned as they are. If every document scores the same, that
is the answer — usually a sign that the query is too vague or that none of the
candidates is relevant — not an error to retry.

## Instructions

Without \`instruction\`, the model judges relevance as web search does: does
this passage answer this query? A task-specific line can help:

~~~json
{
  "model": "${RERANK_MODEL_ID}",
  "instruction": "Given a customer question, find the help-centre article that resolves it",
  "query": "my invoice shows the wrong company name",
  "documents": ["…", "…"]
}
~~~

Keep one instruction per use case: scores produced under different
instructions are not comparable with each other.

## Errors and capacity

| You see | Because | Do |
| --- | --- | --- |
| \`400 invalid_request_error\` | An unsupported field, no documents, more than 100, an instruction over 512 characters, or an \`Idempotency-Key\` header. | Fix the request. |
| \`400 context_length_exceeded\` | A query-and-document pair is over 4,096 tokens; \`param\` names the document. | Split that document. |
| \`503 model_unavailable\` | The engine is down, or at capacity: it also serves the TechSara chat application, which keeps priority, and public requests share a small queue in front of it. | Wait for \`Retry-After\`, add jitter, retry. |
| \`504 timeout\` | The engine did not answer within 60 seconds. | Retry with fewer documents. |

Reranking stores nothing, so an \`Idempotency-Key\` is refused rather than
ignored; the same request scores the same, and a plain retry is safe.

## Usage

Each call is one request in your [usage](/docs/usage), with the input tokens
the engine counted and zero output tokens.
`.trim(),
};
