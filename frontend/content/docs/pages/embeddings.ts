import type { DocPage } from '../types';
import { API_BASE_URL, EMBED_MODEL_ID, EXAMPLE_STATUS } from '../samples';

// 2026-09-13, owner request: every model TechSara runs is on /v1, and the
// embeddings engine is one of them. The shape is the widely used embeddings
// request so an existing client needs a base URL and a key; the differences
// (no `dimensions`, no token-array input, no silent truncation) are listed
// here rather than discovered (CONTRACT §8.4).
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): 2,048 inputs and an 8 MiB body
// (request shape, not usage), lengths checked before any wait so an over-long
// input is always a real 400, and a busy engine waited for rather than refused.
// Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `
~~~http
POST /v1/embeddings
~~~

Requires the \`embeddings.write\` scope. Keys created before 2026-09-13 do not
have it — see [authentication](/docs/authentication#scopes).
`;
const S_CREATE_EMBEDDINGS = `
## Create embeddings

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/embeddings \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${EMBED_MODEL_ID}",
    "input": ["Keys are rotated in the console.", "Webhooks are signed with HMAC-SHA256."]
  }'
~~~
`;
const S_THE_REQUEST = `
## The request

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. \`${EMBED_MODEL_ID}\`. |
| \`input\` | string, or a list of strings | Required. One string, or 1 to 256 strings; none may be empty. |
| \`encoding_format\` | string | Optional. \`float\` (the default) or \`base64\`. |

That is the complete list, and **any other field is a \`400\`** naming it —
\`dimensions\` and \`user\` included. Input given as token-id arrays is a
\`400\` too: send text.

Each input may be up to **4,096 tokens**. A longer one is refused with
\`400 context_length_exceeded\` and \`param\` naming it — \`input.3\` for the
fourth — rather than cut short: a vector for the first half of a document
looks exactly like a vector for the whole of it, and nothing downstream could
tell. Split long documents into passages yourself; you choose better
boundaries than a character count would.

The body is at most 1 MiB.
`;
const S_THE_RESPONSE = `
## The response

~~~json
{
  "object": "list",
  "model": "${EMBED_MODEL_ID}",
  "data": [
    { "object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, 0.0789] },
    { "object": "embedding", "index": 1, "embedding": [-0.0211, 0.0034, 0.0612] }
  ],
  "usage": { "prompt_tokens": 17, "total_tokens": 17 }
}
~~~

* Each \`embedding\` has **1,024** numbers; the sample above is shortened.
* \`index\` is the position of the input it belongs to. The order is preserved,
  but match on \`index\` anyway.
* \`usage\` counts the input tokens. It is \`null\` — never \`0\` — when the
  engine did not report counts.

### base64

With \`"encoding_format": "base64"\`, each \`embedding\` is a string: the
vector as little-endian 32-bit floats, base64-encoded — about a quarter of the
size of the JSON numbers.

~~~python
import base64
import struct

def decode(b64: str) -> list[float]:
    raw = base64.b64decode(b64)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))
~~~
`;
const S_QUERIES_AND_DOCUMENTS = `
## Queries and documents

Text is embedded **exactly as you send it**; the API adds nothing. For
retrieval — a short query against longer passages — this model does best when
the *query* starts with a one-line description of the task and the *documents*
do not:

~~~text
Instruct: Given a web search query, retrieve relevant passages that answer the query
Query: how do I rotate an API key?
~~~

Embed your passages as plain text, store those vectors, and embed each incoming
query with the two-line form above. Use the same instruction for every query in
one index. For symmetric tasks — clustering, deduplication, comparing two
passages — embed everything as plain text.

The vectors are meant for cosine similarity. Compare vectors only from the
same model.
`;
const S_MANY_INPUTS = `
## Many inputs

Up to 256 inputs per request. The server splits a request into smaller engine
calls and sends them in order, so a request of many long inputs takes
proportionally longer — and a synchronous request through the public hostname
must finish within about 100 seconds (see
[long outputs](/docs/long-output#choose-the-mode-before-you-choose-the-size)).
For a large corpus, send modest batches, a few at a time, rather than every
request at the maximum.

~~~python
import os
import httpx

def embed_all(texts: list[str], batch: int = 64) -> list[list[float]]:
    vectors: list[list[float]] = []
    with httpx.Client(
        base_url="${API_BASE_URL}",
        headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
        timeout=120.0,
    ) as api:
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            response = api.post("/embeddings", json={"model": "${EMBED_MODEL_ID}", "input": chunk})
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            vectors.extend(row["embedding"] for row in rows)
    return vectors
~~~
`;
const S_ERRORS_AND_CAPACITY = `
## Errors and capacity

| You see | Because | Do |
| --- | --- | --- |
| \`400 invalid_request_error\` | An unsupported field, an empty input, more than 256 inputs, or an \`Idempotency-Key\` header. | Fix the request. |
| \`400 context_length_exceeded\` | One input is over 4,096 tokens; \`param\` names it. | Split that input. |
| \`503 model_unavailable\` | The engine is down, or at capacity: it also serves the TechSara chat application, which keeps priority, and public requests share a small queue in front of it. | Wait for \`Retry-After\` (a few seconds), add jitter, retry. |
| \`504 timeout\` | The engine did not answer within 60 seconds. | Retry with a smaller batch. |

An \`Idempotency-Key\` header is refused here rather than ignored: an
embeddings call stores nothing that a retry could be matched against, and an
accepted-but-ignored key would promise a safety it does not give. Embedding the
same text twice returns the same vectors, so a plain retry is already safe.
`;
const S_USAGE = `
## Usage

Each call is one request in your [usage](/docs/usage), with the input tokens
the engine counted and zero output tokens — an embedding generates no text.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_REQUEST = `
## The request

| Field | Type | Rule |
| --- | --- | --- |
| \`model\` | string | Required. \`${EMBED_MODEL_ID}\`. |
| \`input\` | string, or a list of strings | Required. One string, or 1 to 2,048 strings; none may be empty. |
| \`encoding_format\` | string | Optional. \`float\` (the default) or \`base64\`. |

That is the complete list, and **any other field is a \`400\`** naming it —
\`dimensions\` and \`user\` included. Input given as token-id arrays is a
\`400\` too: send text.

Each input may be up to **4,096 tokens**. A longer one is refused with
\`400 context_length_exceeded\` and \`param\` naming it — \`input.3\` for the
fourth — rather than cut short: a vector for the first half of a document
looks exactly like a vector for the whole of it, and nothing downstream could
tell. Split long documents into passages yourself; you choose better
boundaries than a character count would. Lengths are checked before the request
waits for anything, so this refusal is always an immediate \`400\`.

The body is at most 8 MiB.
`;
const LATER_MANY_INPUTS = `
## Many inputs

Up to 2,048 inputs per request. The server splits a request into smaller engine
calls and sends them in order, so a request of many long inputs takes
proportionally longer — and that is fine: the API sends a byte at least every
15 seconds while it works, and has no time limit of its own. Leave your
client's read timeout off (see [timeouts](/docs/timeouts)). For a large corpus,
batches of a few hundred make a failed batch cheap to repeat.

~~~python
import os
import httpx

def embed_all(texts: list[str], batch: int = 512) -> list[list[float]]:
    vectors: list[list[float]] = []
    with httpx.Client(
        base_url="${API_BASE_URL}",
        headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
        timeout=httpx.Timeout(10.0, read=None),
    ) as api:
        for start in range(0, len(texts), batch):
            chunk = texts[start:start + batch]
            response = api.post("/embeddings", json={"model": "${EMBED_MODEL_ID}", "input": chunk})
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            vectors.extend(row["embedding"] for row in rows)
    return vectors
~~~
`;
const LATER_ERRORS_AND_CAPACITY = `
## Errors and capacity

| You see | Because | Do |
| --- | --- | --- |
| \`400 invalid_request_error\` | An unsupported field, an empty input, more than 2,048 inputs, or an \`Idempotency-Key\` header. | Fix the request. |
| \`400 context_length_exceeded\` | One input is over 4,096 tokens; \`param\` names it. | Split that input. |
| \`503 model_unavailable\` | The engine is down, or restarted twice while your request ran (then with \`x-should-retry: false\`). | Wait for \`Retry-After\`, add jitter, retry — unless told not to. |

The engine also serves the TechSara chat application, which keeps priority, and
public requests share a small queue in front of it. **A busy engine is waited
for, not refused**: your request waits its turn, with the connection kept
alive. If the engine restarts under a request, the request is sent again for
you.

An \`Idempotency-Key\` header is refused here rather than ignored: an
embeddings call stores nothing that a retry could be matched against, and an
accepted-but-ignored key would promise a safety it does not give. Embedding the
same text twice returns the same vectors, so a plain retry is already safe.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function embeddingsPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, S_CREATE_EMBEDDINGS, LATER_THE_REQUEST, S_THE_RESPONSE, S_QUERIES_AND_DOCUMENTS, LATER_MANY_INPUTS, LATER_ERRORS_AND_CAPACITY, S_USAGE]
    : [INTRO, S_CREATE_EMBEDDINGS, S_THE_REQUEST, S_THE_RESPONSE, S_QUERIES_AND_DOCUMENTS, S_MANY_INPUTS, S_ERRORS_AND_CAPACITY, S_USAGE];
  return {
    slug: 'embeddings',
    title: 'Embeddings',
    summary: noTimeout
      ? 'POST /v1/embeddings turns up to 2,048 texts into 1,024-dimension vectors ' +
        'with techsara-embed, for search, clustering and deduplication.'
      : 'POST /v1/embeddings turns text into 1,024-dimension vectors with ' +
      'techsara-embed, for search, clustering and deduplication.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const embeddings: DocPage = embeddingsPage({ noTimeout: NO_TIMEOUT_LIVE });
