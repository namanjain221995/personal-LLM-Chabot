import type { DocPage } from '../types';
import { EXAMPLE_STATUS } from '../samples';

export const errors: DocPage = {
  slug: 'errors',
  title: 'Errors',
  summary:
    'One envelope everywhere, a closed list of codes, and a clear line ' +
    'between what you should retry and what you should fix.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
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

## The codes

| Code | Status | When |
| --- | --- | --- |
| \`invalid_request_error\` | 400 | Malformed, out of range, or a field we cannot honour. |
| \`context_length_exceeded\` | 400 | The prompt is over the model's input ceiling. |
| \`invalid_api_key\` | 401 | Missing, malformed, unknown, revoked or expired key — or a live key whose service account, project or workspace is disabled, or which was sent from outside the project's IP allowlist. |
| \`insufficient_scope\` | 403 | The key is valid but lacks the scope. |
| \`origin_not_allowed\` | 403 | Browser \`Origin\` outside the project's allowlist. |
| \`model_not_found\` | 404 | Unknown model, or one this key may not use. |
| \`response_not_found\` | 404 | Not this project's response. |
| \`idempotency_conflict\` | 409 | Same \`Idempotency-Key\`, different body. |
| \`request_too_large\` | 413 | Body over 1 MiB. |
| \`rate_limit_error\` | 429 | Requests or tokens per minute exceeded. |
| \`quota_exceeded\` | 429 | The daily token quota is exhausted. |
| \`concurrency_limit_exceeded\` | 429 | Too many requests in flight. |
| \`model_recovering\` | 503 | The engine is restarting. Retry-safe. |
| \`model_unavailable\` | 503 | The engine is down. |
| \`timeout\` | 504 | Generation exceeded the wall clock. |
| \`internal_error\` | 500 | Anything else. Never a traceback. |

A path under \`/v1\` that is not one of the published endpoints answers
\`404\`, and a method an endpoint does not accept answers \`405\` — both in
this same envelope, with the code \`invalid_request_error\`, rather than a
bare framework error your parser has never seen.

### Types

\`invalid_request_error\`, \`authentication_error\`, \`permission_error\`,
\`rate_limit_error\`, \`service_unavailable_error\`, \`timeout_error\`,
\`server_error\`.

All three 429 codes share the \`rate_limit_error\` type on purpose: your
reaction to every one of them is the same — wait for \`Retry-After\`, then
retry — so one type means one retry branch rather than three near-identical
ones.

## What to retry

Retry, unchanged, with backoff:

\`rate_limit_error\`, \`quota_exceeded\`, \`concurrency_limit_exceeded\`,
\`model_recovering\`, \`model_unavailable\`, \`timeout\`.

The five \`429\` and \`503\` codes carry \`Retry-After\`, in whole seconds and
never less than \`1\`. Honour it, add jitter, and cap your attempts.
\`timeout\` is a \`504\` and carries no \`Retry-After\`: the generation ran out
of wall clock, so back off on your own schedule — and before you retry,
consider [streaming](/docs/streaming) or a
[background response](/docs/background), which a long generation suits
better.

Do **not** retry a \`400\`, \`401\`, \`403\`, \`404\`, \`409\` or \`413\`
unchanged: nothing about the next identical attempt will be different. Fix
the request, the key or the scope.

A \`500\` is ours. Retry once with a fresh
[idempotency key](/docs/idempotency) if the operation is safe to repeat, and
send us the \`request_id\` if it persists.

~~~python
import random
import time

RETRYABLE = {
    "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
    "model_recovering", "model_unavailable", "timeout",
}

def send_with_retry(client, payload, *, attempts=5):
    for attempt in range(attempts):
        response = client.post("/responses", json=payload)
        if response.status_code < 400:
            return response.json()

        code = response.json().get("error", {}).get("code", "")
        if code not in RETRYABLE or attempt == attempts - 1:
            response.raise_for_status()

        # Honour Retry-After, then add jitter so a fleet of clients does not
        # come back in lockstep and cause the next rate limit itself.
        wait = float(response.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait + random.uniform(0, 1))
    raise RuntimeError("unreachable")
~~~

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

## Mid-stream

Once a stream has started, the HTTP status is already committed, so a failure
arrives in the stream instead, carrying the same \`code\` vocabulary: a
\`response.failed\` event whose response object carries an \`error\` naming
the code, or — when
there is no response to describe — an \`error\` event with \`code\`,
\`message\` and \`param\` at its top level. The request id is not repeated
there; it arrived as \`X-Request-Id\` before the first byte. See
[streaming](/docs/streaming#errors-mid-stream).

## What an error will never contain

No traceback, no SQL, no environment value, no container name, no internal
hostname, no filesystem path, no private IP address. If a message ever looks
like machinery, report it.
`.trim(),
};
