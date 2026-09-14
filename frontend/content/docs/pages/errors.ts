import type { DocPage } from '../types';
import { EXAMPLE_STATUS } from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';
import { SIDECARS_NO_TIMEOUT_LIVE } from './sidecarsLive';

// 2026-09-13, no-timeout design (revision 2): capacity is never a refusal, a
// request is never ended by a clock, a 409 is only ever a real conflict, and
// `x-should-retry: false` marks the failures an SDK must not re-send. Built in
// either state from NO_TIMEOUT_LIVE.

/**
 * The Files API's rows of the closed table (2026-09-14): `publicapi/errors.py`
 * holds them since the Files routes joined CONTRACT §7, so this page lists
 * them in both states. They name no route, which the Files pages (held back by
 * FILES_API_PUBLISHED) describe.
 */
const FILES_CODE_ROWS = `| \`file_not_found\` | 404 | A file id that is absent, malformed, deleted, expired or another project's — all one answer — or a derived-output name that does not exist. |
| \`upload_not_found\` | 404 | An upload id that is absent, malformed or another project's. |
| \`file_not_ready\` | 409 | A file whose bytes are still being assembled, or derived output asked for while processing is queued or running. Carries \`Retry-After\`. Derived output of a file in \`error\` is a \`400\` instead, and not worth retrying. |
| \`upload_state_conflict\` | 409 | A part, \`complete\` or \`cancel\` for an upload in the wrong state. Sent with \`x-should-retry: false\`, except for a \`complete\` that meets another one still being recorded. |
| \`checksum_mismatch\` | 400 | A part's SHA-256 does not match the bytes that arrived. |
| \`incomplete_body\` | 408 | The connection closed before a file or part body was complete. Nothing was recorded; send it again. |
| \`storage_unavailable\` | 503 | The service cannot take new file bytes right now. Type \`api_error\`; \`x-should-retry\` says whether a retry can succeed. |`;

const INTRO = `

`;
const S_THE_ENVELOPE = `
## The envelope

Every failure — HTTP or mid-stream — has the same shape:

~~~json
{
  "error": {
    "message": "The API key is invalid.",
    "type": "authentication_error",
    "code": "invalid_api_key",
    "param": null,
    "request_id": "req_…"
  }
}
~~~

| Field | Use it for |
| --- | --- |
| \`code\` | Switch on this. It is the precise reason and it is stable. |
| \`type\` | The coarse class, when you want one branch for a family. |
| \`message\` | Show a human. Do not parse it. |
| \`param\` | The offending field, as a JSON path (\`metadata.customer_id\`) when one field is at fault. |
| \`request_id\` | Log it. It is how we find your request. |

The same value is on every response as \`X-Request-Id\`, success included.
`;
/** Today's rows about embeddings, rerank and transcriptions, which shipped
 * without a clock on 2026-09-14 (SIDECARS_NO_TIMEOUT_LIVE). */
const tooLargeToday = (sidecars: boolean): string =>
  sidecars
    ? 'Body over its endpoint\'s limit — 1 MiB, 8 MiB for embeddings and rerank, 20 MiB with images, 90 MiB with audio — or text over 1 MiB, or an audio file over 89 MiB.'
    : 'Body over its endpoint\'s limit — 1 MiB, 20 MiB with images, 26 MiB with audio — or text over 1 MiB, audio over 25 MiB or 300 seconds.';
const unavailableToday = (sidecars: boolean): string =>
  sidecars
    ? 'The engine is down, or at capacity: its queue, shared with the chat application, did not free a place in time. Embeddings, rerank and transcriptions wait for a place instead; for embeddings and rerank this is also the server holding as much work in memory as it safely can, or an input that stopped the engine twice (then with \`x-should-retry: false\`). Retry-safe unless told not to.'
    : 'The engine is down, or at capacity: its queue, shared with the chat application, did not free a place in time. Retry-safe.';
const timeoutToday = (sidecars: boolean): string =>
  sidecars
    ? 'Generation exceeded its wall clock. Embeddings, rerank and transcriptions are never ended by a clock.'
    : 'Generation exceeded its wall clock, or an engine did not answer in time.';

const theCodesToday = (sidecars: boolean): string => `
## The codes

| Code | Status | When |
| --- | --- | --- |
| \`invalid_request_error\` | 400 | Malformed, out of range, or a field we cannot honour — including a model sent to an endpoint it does not serve (\`param\` \`model\`) and an image that is not an accepted \`data:\` URL. |
| \`context_length_exceeded\` | 400 | The prompt is over the model's input ceiling, or one [embeddings](/docs/embeddings) input or [rerank](/docs/rerank) document is too long (\`param\` names it). |
| \`invalid_api_key\` | 401 | Missing, malformed, unknown, revoked or expired key — or a live key whose service account, project or workspace is disabled, or which was sent from outside the project's IP allowlist. |
| \`insufficient_scope\` | 403 | The key is valid but lacks the scope. |
| \`origin_not_allowed\` | 403 | Browser \`Origin\` outside the project's allowlist. |
| \`model_not_found\` | 404 | Unknown model, one this key may not use, or one this deployment does not run. |
| \`response_not_found\` | 404 | Not this project's response. |
| \`idempotency_conflict\` | 409 | Same \`Idempotency-Key\`, different body — or the same body while the first request with that key is still running, which carries \`Retry-After\`. |
| \`request_too_large\` | 413 | ${tooLargeToday(sidecars)} An image over 10 MiB is a \`400\`, like every other image rule. |
| \`rate_limit_error\` | 429 | Only if an operator has [enabled limits](/docs/rate-limits#if-an-operator-enables-limits): requests or tokens per minute exceeded. |
| \`quota_exceeded\` | 429 | Only if an operator has enabled limits: the daily token quota is exhausted. |
| \`concurrency_limit_exceeded\` | 429 | Only if an operator has enabled limits: too many of the project's requests in flight. |
| \`model_recovering\` | 503 | The engine is restarting. Retry-safe. |
| \`model_unavailable\` | 503 | ${unavailableToday(sidecars)} |
| \`timeout\` | 504 | ${timeoutToday(sidecars)} |
| \`internal_error\` | 500 | Anything else. Never a traceback. |
${FILES_CODE_ROWS}

A path under \`/v1\` that is not one of the published endpoints answers
\`404\`, and a method an endpoint does not accept answers \`405\` — both in
this same envelope, with the code \`invalid_request_error\`, rather than a
bare framework error your parser has never seen.

### Types

\`invalid_request_error\`, \`authentication_error\`, \`permission_error\`,
\`rate_limit_error\`, \`service_unavailable_error\`, \`timeout_error\`,
\`server_error\`, \`api_error\`.

The API enforces **no usage limits** today — no request, token, daily or
concurrency limit (see [rate limits](/docs/rate-limits)) — so no request is
refused with a \`429\`. The two refusals that remain are not limits either,
and neither is spelled as one: an [idempotency key](/docs/idempotency) whose
first request is still running is a \`409 idempotency_conflict\` with
\`Retry-After\` — "come back in two seconds for the answer" — and an engine
whose shared queue is too deep to join is a \`503 model_unavailable\` — the
engine's capacity, the same for every caller. That \`503\` can come from any of
the six models: each engine the TechSara chat application also uses has a small
public queue in front of it, the chat application keeps priority, and a request
that does not get a place within about 30 seconds is refused with
\`Retry-After\` — a few seconds for most engines, a minute for a very long
generation on \`techsara-35b\` (see
[capacity queues](/docs/rate-limits#capacity-queues-per-engine)). The three \`429\` codes
stay in the vocabulary for deployments whose operator turns limits on, and
share the \`rate_limit_error\` type on purpose: your reaction to every one of
them is the same — wait for \`Retry-After\`, then retry — so one type means
one retry branch rather than three near-identical ones.
`;
const S_WHAT_TO_RETRY = `
## What to retry

Retry, unchanged, with backoff:

\`rate_limit_error\`, \`quota_exceeded\`, \`concurrency_limit_exceeded\`,
\`model_recovering\`, \`model_unavailable\`, \`timeout\`. Of those, the
three \`429\` codes arrive only where an operator enables limits. Also retry a
\`409\` that carries \`Retry-After\`: it is your own earlier request with the
same idempotency key, still running, and the retry collects its answer.

The five \`429\` and \`503\` codes, and a still-running \`409\`, carry \`Retry-After\`, in whole seconds and
never less than \`1\`. Honour it, add jitter, and cap your attempts.
\`timeout\` is a \`504\` and carries no \`Retry-After\`: the generation ran out
of wall clock, so back off on your own schedule — and before you retry,
consider [streaming](/docs/streaming) or a
[background response](/docs/background), which a long generation suits
better. A stream or a background response that times out **keeps the text it
had written** and reports the tokens it spent, so you may not need to retry the
whole of it — see [long outputs](/docs/long-output#the-wall-clock).

Do **not** retry a \`400\`, \`401\`, \`403\`, \`404\`, \`413\` or a
\`409\` without \`Retry-After\` unchanged: nothing about the next identical attempt will be different. Fix
the request, the key or the scope.

A \`500\` is ours. Retry once with a fresh
[idempotency key](/docs/idempotency) if the operation is safe to repeat, and
send us the \`request_id\` if it persists.

~~~python
import random
import time

RETRYABLE = {
    # quota_exceeded and concurrency_limit_exceeded arrive only if an operator
    # enables limits; listing them costs nothing.
    "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
    "model_recovering", "model_unavailable", "timeout",
}

def send_with_retry(client, payload, *, attempts=5):
    for attempt in range(attempts):
        response = client.post("/responses", json=payload)
        if response.status_code < 400:
            return response.json()

        code = response.json().get("error", {}).get("code", "")
        # A 409 with Retry-After is this key's first request, still running.
        running = response.status_code == 409 and "Retry-After" in response.headers
        if (code not in RETRYABLE and not running) or attempt == attempts - 1:
            response.raise_for_status()

        # Honour Retry-After, then add jitter so a fleet of clients does not
        # come back in lockstep and cause the next spike themselves.
        wait = float(response.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait + random.uniform(0, 1))
    raise RuntimeError("unreachable")
~~~
`;
const S_401_403_AND_404_ARE_DIFFERENT_QUESTIONS = `
## 401, 403 and 404 are different questions

* \`401\` — *who are you?* The credential is missing, malformed, revoked or
  expired — or it is a real key that cannot be used right now, because its
  service account, project or workspace is disabled or the request came from
  outside the project's IP allowlist. All of those are the *same* \`401\` with
  the same message, on purpose: an answer that said "revoked" or "project
  disabled" would tell whoever holds a leaked key that it is real. Check the
  key, then ask your administrator about the project.
* \`403\` — *you are known, but no.* Either the key lacks the scope
  (\`insufficient_scope\`, with a \`WWW-Authenticate\` challenge naming what
  is required) or the browser origin is not allowed.
* \`404\` on a model — *we will not say.* A model your key may not use is
  indistinguishable from one that does not exist. That is intentional: a
  \`403\` there would confirm the model's existence to a caller with no right
  to know.
`;
const S_MID_STREAM = `
## Mid-stream

Once a stream has started, the HTTP status is already committed, so a failure
arrives in the stream instead, carrying the same \`code\` vocabulary: a
\`response.failed\` event whose response object carries an \`error\` naming
the code, or — when
there is no response to describe — an \`error\` event with \`code\`,
\`message\` and \`param\` at its top level. The request id is not repeated
there; it arrived as \`X-Request-Id\` before the first byte. See
[streaming](/docs/streaming#errors-mid-stream).
`;
const S_WHAT_AN_ERROR_WILL_NEVER_CONTAIN = `
## What an error will never contain

No traceback, no SQL, no environment value, no container name, no internal
hostname, no filesystem path, no private IP address. If a message ever looks
like machinery, report it.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_CODES = `
## The codes

| Code | Status | When |
| --- | --- | --- |
| \`invalid_request_error\` | 400 | Malformed, out of range, or a field we cannot honour — including a model sent to an endpoint it does not serve (\`param\` \`model\`), an image that is not an accepted \`data:\` URL, and \`starting_after\` without \`stream=true\`, or a replay of events that are no longer kept (\`param\` \`stream\`). |
| \`context_length_exceeded\` | 400 | The prompt is over the model's input ceiling, or one [embeddings](/docs/embeddings) input or [rerank](/docs/rerank) document is too long (\`param\` names it). Checked before any waiting, so it is always a real \`400\`. |
| \`invalid_api_key\` | 401 | Missing, malformed, unknown, revoked or expired key — or a live key whose service account, project or workspace is disabled, or which was sent from outside the project's IP allowlist. A key revoked while its request runs ends that request with this code. |
| \`insufficient_scope\` | 403 | The key is valid but lacks the scope. |
| \`origin_not_allowed\` | 403 | Browser \`Origin\` outside the project's allowlist. |
| \`model_not_found\` | 404 | Unknown model, one this key may not use, or one this deployment does not run. |
| \`response_not_found\` | 404 | Not this project's response — or, for a replayed stream, not created by this key or its service account. |
| \`idempotency_conflict\` | 409 | Same \`Idempotency-Key\` with a different body, or from a key of a different service account. Sent with \`x-should-retry: false\`. |
| \`request_too_large\` | 413 | Body over its endpoint's limit — 1 MiB, 8 MiB for embeddings and rerank, 20 MiB with images, 90 MiB with audio — or text over 1 MiB. An image over 10 MiB is a \`400\`, like every other image rule. |
| \`rate_limit_error\` | 429 | Only if an operator has [enabled limits](/docs/rate-limits#if-an-operator-enables-limits): requests or tokens per minute exceeded. |
| \`quota_exceeded\` | 429 | Only if an operator has enabled limits: the daily token quota is exhausted. |
| \`concurrency_limit_exceeded\` | 429 | Only if an operator has enabled limits: too many of the project's requests in flight. |
| \`model_recovering\` | 503 | The engine is restarting. Retry-safe. |
| \`model_unavailable\` | 503 | The engine is down, or a physical safeguard of the service tripped — too many open connections, too little free disk, or as much embedding and rerank work in memory as the server safely holds. Never "busy": a busy engine is waited for. Retry-safe unless it carries \`x-should-retry: false\`. |
| \`timeout\` | 504 | Kept in the vocabulary; no request on \`/v1\` is ended by a clock. |
| \`internal_error\` | 500 | Anything else. Never a traceback. |
${FILES_CODE_ROWS}

A path under \`/v1\` that is not one of the published endpoints answers
\`404\`, and a method an endpoint does not accept answers \`405\` — both in
this same envelope, with the code \`invalid_request_error\`, rather than a
bare framework error your parser has never seen.

### Types

\`invalid_request_error\`, \`authentication_error\`, \`permission_error\`,
\`rate_limit_error\`, \`service_unavailable_error\`, \`timeout_error\`,
\`server_error\`, \`api_error\`.

The API enforces **no usage limits** today — no request, token, daily or
concurrency limit (see [rate limits](/docs/rate-limits)) — so no request is
refused with a \`429\`, and **no request is refused for being busy either**: a
request that has to wait for an engine waits, with the connection kept alive
(see [timeouts](/docs/timeouts#waiting-for-capacity)). The three \`429\` codes
stay in the vocabulary for deployments whose operator turns limits on, and
share the \`rate_limit_error\` type on purpose: your reaction to every one of
them is the same.

## x-should-retry

Some failures carry \`x-should-retry: false\`. Both \`openai\` client libraries
read the header before their own retry rules, and so should your client: it
marks a failure that re-sending the same request cannot fix, or that could run
the work twice.

* \`409 idempotency_conflict\` — a different body, or a different credential.
* A \`500\` after a generation without an \`Idempotency-Key\` had started.
* A generation that failed after being caught up in two engine crashes.
* An embeddings or rerank input that stopped its engine twice — and, for an
  hour after, any request that sends that input again.
* A transcription that failed because its engine restarted twice while it ran.
* A \`503\` from the API's edge when the request may already have been received
  and carried no \`Idempotency-Key\`.

A \`200\` whose JSON body says \`"status": "failed"\` — or \`"choices": []\`
with an \`error\` — is a failure too: a synchronous answer that runs past 15
seconds sends its \`200\` before it knows how it will end. See
[long synchronous calls](/docs/timeouts#long-synchronous-calls).
`;
const LATER_WHAT_TO_RETRY = `
## What to retry

Retry, unchanged, with backoff:

\`model_recovering\`, \`model_unavailable\` — and \`rate_limit_error\`,
\`quota_exceeded\` and \`concurrency_limit_exceeded\`, which arrive only where
an operator enables limits. Also retry a dropped connection, a \`502\`, and the
\`524\` or \`530\` a proxy in front of the API can answer, reusing the same
[idempotency key](/docs/idempotency) so the retry joins the request instead of
repeating it.

The \`429\` and \`503\` codes carry \`Retry-After\`, in whole seconds and never
less than \`1\` — at most 60 on a \`503\`. Honour it, add jitter, and cap your
attempts by time rather than by count: keep trying for at least ten minutes. A
\`504 timeout\` carries no \`Retry-After\`.

Never retry a response that carries \`x-should-retry: false\`, and do **not**
retry a \`400\`, \`401\`, \`403\`, \`404\`, \`409\` or \`413\` unchanged: nothing
about the next identical attempt will be different. Fix the request, the key or
the scope.

A \`500\` is ours. Retry once with the same idempotency key if it did not say
\`x-should-retry: false\`, and send us the \`request_id\` if it persists.

~~~python
import random
import time

RETRYABLE = {
    # quota_exceeded and concurrency_limit_exceeded arrive only if an operator
    # enables limits; listing them costs nothing.
    "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
    "model_recovering", "model_unavailable",
}

def send_with_retry(client, payload, key, *, give_up_after_s=600):
    deadline = time.monotonic() + give_up_after_s
    attempt = 0
    while True:
        response = client.post("/responses", json=payload, headers={"Idempotency-Key": key})
        if response.status_code < 400:
            body = response.json()
            if body.get("status") == "failed":        # a 200 can carry a failure
                raise RuntimeError(body["error"]["code"])
            return body

        code = response.json().get("error", {}).get("code", "")
        final = response.headers.get("x-should-retry") == "false" or code not in RETRYABLE
        if final or time.monotonic() > deadline:
            response.raise_for_status()

        # Honour Retry-After, then add jitter so a fleet of clients does not
        # come back in lockstep and cause the next spike themselves.
        wait = float(response.headers.get("Retry-After", min(60, 2 ** attempt)))
        time.sleep(wait + random.uniform(0, 1))
        attempt += 1
~~~
`;
const LATER_MID_STREAM = `
## Mid-stream

Once a stream has started, the HTTP status is already committed, so a failure
arrives in the stream instead, carrying the same \`code\` vocabulary: a
\`response.failed\` event whose response object carries an \`error\` naming
the code, or — when there is no response to describe — an \`error\` event with
\`code\`, \`message\` and \`param\` at its top level. The request id is not
repeated there; it arrived as \`X-Request-Id\` before the first byte. See
[streaming](/docs/streaming#errors-mid-stream).

A stream that simply stops, with no terminal event, has not failed: the
response is still running. [Resume it](/docs/timeouts#resuming-a-stream).
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function errorsPage({
  noTimeout,
  sidecarsLive = SIDECARS_NO_TIMEOUT_LIVE,
}: {
  noTimeout: boolean;
  sidecarsLive?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [INTRO, S_THE_ENVELOPE, LATER_THE_CODES, LATER_WHAT_TO_RETRY, S_401_403_AND_404_ARE_DIFFERENT_QUESTIONS, LATER_MID_STREAM, S_WHAT_AN_ERROR_WILL_NEVER_CONTAIN]
    : [INTRO, S_THE_ENVELOPE, theCodesToday(sidecarsLive), S_WHAT_TO_RETRY, S_401_403_AND_404_ARE_DIFFERENT_QUESTIONS, S_MID_STREAM, S_WHAT_AN_ERROR_WILL_NEVER_CONTAIN];
  return {
    slug: 'errors',
    title: 'Errors',
    summary:
      'One envelope everywhere, a closed list of codes, and a clear line ' +
      'between what you should retry and what you should fix.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const errors: DocPage = errorsPage({ noTimeout: NO_TIMEOUT_LIVE });
