import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): a request that repeats a running
// request's key no longer gets a 409 to come back later — it attaches to the
// generation and receives its answer. Attaching is limited to the key that
// created it (or its service account), and an SDK retry of an identical body
// attaches even without a key. Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `
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
`;
const S_THE_RULES = `
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
`;
const S_CHOOSING_A_KEY = `
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
`;
const S_WHAT_IT_DOES_NOT_DO = `
## What it does not do

* It does not make a **bad** request succeed. A \`400\` is a \`400\` again.
* It does not span projects or endpoints: the same string on
  \`/v1/chat/completions\` is a different claim from the one on
  \`/v1/responses\`.
* It does not last forever. Past 24 hours, retry semantics are gone.
* It is not needed for \`GET\` or for
  [cancellation](/docs/responses#cancel-one), which is idempotent by
  construction.
`;
const S_PAIRING_IT_WITH_BACKGROUND_WORK = `
## Pairing it with background work

A [background response](/docs/background) plus an idempotency key is the
sturdy combination for anything expensive: the \`202\` is durable before the
work starts, and a retry after a dropped connection returns the same response
id rather than starting the job again.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_RULES = `
## The rules

| You send | You get |
| --- | --- |
| A new key | The request runs normally. |
| The same key and body **while the first request is still running** | Your request **attaches** to it. A synchronous call waits for the same answer; a stream replays from its first event and then follows live; a background request gets the \`202\` with the response as it stands. The model runs once. |
| The same key and body, after the original finished | A replay of the original, in the original's shape, with its id, \`status\` and \`usage\`. The model is not invoked twice. |
| The same key and body, after the original **failed** or was **refused before it ran** — a \`503\`, say | Nothing to replay: the key is released, and your retry runs. |
| The same key, a **different** body | \`409 idempotency_conflict\` with \`x-should-retry: false\`: retrying will never succeed. |
| The same key, sent with a **different API key** that is not of the same service account | \`409 idempotency_conflict\` with \`x-should-retry: false\`. A key is never a way to read another credential's answer. |
| A key that is empty, longer than 255 characters, or not printable ASCII | \`400 invalid_request_error\` with \`param\` \`Idempotency-Key\` — never silently treated as "no key". |

Keys are scoped to your project and the endpoint, and are retained for
**24 hours**. After that the same key is simply a new one.

**What a replay gives back.** A replay while the original's stream events are
still kept — as it runs, and for one hour after it ends — carries the text: a
Responses body, a \`chat.completion\`, or the stream replayed chunk for chunk.
After that it is rebuilt from the stored record, and only a
[background response](/docs/background) stores its output, so a later replay of
a synchronous or streamed request tells you that it ran and what it cost, with
an empty \`output\` or a \`null\` message \`content\`. A replay is still a
request: it is recorded in the project's [usage](/docs/usage) like any other.

The conflict case is the important one. A key is a promise that two requests
are the same request; if the body differs, one of them is a mistake, and
guessing which would be worse than refusing.

**Long generations attach for as long as they run.** A retry an hour into a
four-hour generation joins it and receives the rest; it does not start a second
one, and there is no lease after which a running request's key is handed over.

**Retries without a key.** The \`openai\` client libraries mark their own
automatic retries. When one arrives from the same API key with a byte-identical
body, for a request that lost its client or was paused by a restart less than
an hour ago, it attaches too. Your own retry loop is not marked, and a body
that differs by one byte is a new request — so send a key whenever a duplicate
would cost you.
`;
const LATER_WHAT_IT_DOES_NOT_DO = `
## What it does not do

* It does not make a **bad** request succeed. A \`400\` is a \`400\` again.
* It does not span projects, endpoints or credentials: the same string on
  \`/v1/chat/completions\` is a different claim from the one on
  \`/v1/responses\`, and another key cannot use it to attach.
* It does not last forever. Past 24 hours, retry semantics are gone.
* It is not needed for \`GET\` or for
  [cancellation](/docs/responses#cancel-one), which is idempotent by
  construction.
`;
const LATER_PAIRING_IT_WITH_BACKGROUND_WORK = `
## Pairing it with background work

A [background response](/docs/background) plus an idempotency key is the
sturdy combination for anything expensive: the \`202\` is durable before the
work starts, a retry after a dropped connection returns the same response id,
and the job carries on through a restart of the service.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function idempotencyPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, LATER_THE_RULES, S_CHOOSING_A_KEY, LATER_WHAT_IT_DOES_NOT_DO, LATER_PAIRING_IT_WITH_BACKGROUND_WORK]
    : [INTRO, S_THE_RULES, S_CHOOSING_A_KEY, S_WHAT_IT_DOES_NOT_DO, S_PAIRING_IT_WITH_BACKGROUND_WORK];
  return {
    slug: 'idempotency',
    title: 'Idempotency',
    summary: noTimeout
      ? 'Retry a generation safely: the same key and the same body joins the ' +
        'running request, or replays the finished one, instead of a second bill.'
      : 'Retry a generation safely: the same key and the same body gives you the ' +
      'original response instead of a second bill.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const idempotency: DocPage = idempotencyPage({ noTimeout: NO_TIMEOUT_LIVE });
