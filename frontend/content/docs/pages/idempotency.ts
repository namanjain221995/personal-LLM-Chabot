import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const idempotency: DocPage = {
  slug: 'idempotency',
  title: 'Idempotency',
  summary:
    'Retry a generation safely: the same key and the same body gives you the ' +
    'original response instead of a second bill.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
A network timeout does not tell you whether the request landed. Without an
idempotency key, a retry is a second generation: a second bill, a second
answer, and — if a person is watching — a duplicate.

Send an \`Idempotency-Key\` header on any \`POST\` you might repeat:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: 6d1f0f7c-2a4e-4f7b-9a44-0d0c0b7f2b11" \\
  -d '{"model": "${MODEL_ID}", "input": "Explain RAG."}'
~~~

Supported on [\`POST /v1/responses\`](/docs/responses) and
[\`POST /v1/chat/completions\`](/docs/chat-completions) — the two endpoints
where a retry could run a generation twice.

**Not accepted on** [\`POST /v1/embeddings\`](/docs/embeddings),
[\`POST /v1/rerank\`](/docs/rerank) or
[\`POST /v1/audio/transcriptions\`](/docs/audio-transcriptions): the header
there is a \`400 invalid_request_error\` with \`param\` \`Idempotency-Key\`.
Those calls store nothing a retry could be matched against, so a key would be
a promise the server cannot keep — and they return the same result for the
same input, so a plain retry is already safe.

## The rules

| You send | You get |
| --- | --- |
| A new key | The request runs normally. |
| The same key, the same body, after the original finished | A replay of the original, in the original's shape. The model is not invoked twice. |
| The same key, the same body, after the original **failed** or was **refused before it ran** — a \`503\`, say | Nothing to replay: the key is released, and your retry runs. A failure is not kept for 24 hours to be handed back to a client that did exactly what \`Retry-After\` told it to. |
| The same key, while the first is still running | \`409 idempotency_conflict\`, "A request with this Idempotency-Key is still running", with a short \`Retry-After\`. Retry after it to collect the answer. |
| The same key, a **different** body | \`409 idempotency_conflict\`, with no \`Retry-After\`: retrying will never succeed. |
| A key that is empty, longer than 255 characters, or not printable ASCII | \`400 invalid_request_error\` with \`param\` \`Idempotency-Key\` — never silently treated as "no key". |

Keys are scoped to your project and the endpoint, and are retained for
**24 hours**. After that the same key is simply a new one.

**What a replay gives back.** The original's shape, rebuilt from its stored
record: a Responses body from \`/v1/responses\`, a \`chat.completion\` from
\`/v1/chat/completions\`, and a short event stream if the original streamed.
It carries the original id, \`status\` and \`usage\`. What it cannot carry is
text that was never kept: only a [background response](/docs/background)
stores its output, so replaying a synchronous or streamed request tells you
that it ran and what it cost — an empty \`output\`, a \`null\` message
\`content\`, or a stream with no deltas — rather than handing you the answer a
second time. A background request replays as its \`202\` with the response as
it stands now. If you need the answer itself to survive a lost connection,
send the request with \`"background": true\` and the same key.

A replay is still a request: it is recorded in the project's
[usage](/docs/usage) like any other.

The conflict case is the important one. A key is a promise that two requests
are the same request; if the body differs, one of them is a mistake, and
guessing which would be worse than refusing.

The still-running case is the second most important. Telling you to come back
in a couple of seconds is honest and cheap; running the model a second time
would be neither. This \`409\` is not a usage limit — the API enforces
none — so honour \`Retry-After\` and retry with the same key, and you will get the original response.

**Long generations stay "still running" for as long as they run.** A request for
hundreds of thousands of output tokens can take hours, and every retry with its
key gets the \`409\` until it ends. That is the key doing its job: without it,
a client that gave up after two hours and retried would start a second
multi-hour generation. The one case the key cannot see is the service
restarting under the original: the claim is then only released after 13 hours
(well inside the 24-hour retention), so a retry with the same key can keep
answering \`409\` until then. When a stream dropped or a background response
failed with "The service restarted while this response was running.", retry
with a **new** key. For work that long, prefer [background](/docs/background):
its \`202\` hands you the response id straight away.

## Choosing a key

* One key per **logical operation**, not per attempt. Every retry of the same
  operation sends the same key.
* A UUID version 4 is the obvious choice. Anything unique and unguessable
  works.
* Derive it from your own domain object — the order id, the ticket id, the
  job id — so the retry that happens in a different process still sends the
  same key.

~~~python
import uuid

def summarise(ticket_id: str, text: str, client):
    # The key belongs to the ticket, not to this attempt: a retry from a
    # different worker, after a crash, must present the same one.
    idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ticket:{ticket_id}"))
    return client.post(
        "/responses",
        headers={"Idempotency-Key": idempotency_key},
        json={"model": "${MODEL_ID}", "input": text},
    ).json()
~~~

## What it does not do

* It does not make a **bad** request succeed. A \`400\` is a \`400\` again.
* It does not span projects or endpoints: the same string on
  \`/v1/chat/completions\` is a different claim from the one on
  \`/v1/responses\`.
* It does not last forever. Past 24 hours, retry semantics are gone.
* It is not needed for \`GET\` or for
  [cancellation](/docs/responses#cancel-one), which is idempotent by
  construction.

## Pairing it with background work

A [background response](/docs/background) plus an idempotency key is the
sturdy combination for anything expensive: the \`202\` is durable before the
work starts, and a retry after a dropped connection returns the same response
id rather than starting the job again.
`.trim(),
};
