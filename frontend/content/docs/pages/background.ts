import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';

export const background: DocPage = {
  slug: 'background',
  title: 'Background responses',
  summary:
    'Hand the work over, get an id back immediately, and collect the answer ' +
    'by polling or by webhook.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
Some work outlives the request that asked for it: a long document, a
serverless function with a hard ceiling, a mobile client on a bad connection.
Send \`"background": true\` and the platform takes the job.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: 3b6e2a90-8c41-4d5f-b2e7-91f0c4a6d825" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Summarise the attached policy in 300 words.",
    "background": true
  }'
~~~

You get **202 Accepted** and a response object whose \`status\` is
\`queued\`:

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789200000,
  "status": "queued",
  "model": "${MODEL_ID}",
  "output": [],
  "usage": null
}
~~~

The \`202\` is sent **after the row is durable and before any expensive
work**. That ordering is the promise: if you hold an id, the work exists and
will survive your disconnect — and a crash between "accepted" and "started"
cannot lose a job you were told we had.

\`stream\` and \`background\` cannot both be true. There is no stream to
attach to.

There is no cap on how many background responses a project runs at once —
the API enforces no concurrency [limits](/docs/rate-limits). They share the
engine with everything else, so a thousand submitted together are accepted
together and then served as fast as the engine can serve them. A job that
cannot get a place in the engine's shared queue ends \`failed\` with
\`model_unavailable\` — the engine's capacity, not a limit on your
project — and can be submitted again.

## Collecting the answer

### Poll

~~~bash
curl ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID} \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`status\` walks \`queued\` → \`in_progress\` → \`completed\`, or ends at
\`failed\` or \`cancelled\`. On a failure the response carries an \`error\`
object with the same \`code\` vocabulary as the [HTTP
envelope](/docs/errors).

Poll politely — a few seconds between attempts, with a ceiling — and
remember that a poll is a request like any other: it is recorded in your
[usage](/docs/usage), and a tight loop spends the engine's shared capacity
on nothing.

~~~python
import time

def wait_for(response_id, client, *, timeout_s=900, interval_s=3):
    """Poll until the response reaches a terminal status."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/responses/{response_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(interval_s)
    raise TimeoutError(response_id)
~~~

### Or be told

A [webhook](/docs/webhooks) endpoint on your project turns the poll loop into
a delivery: \`response.completed\`, \`response.failed\` and
\`response.cancelled\` are the subscribable events. The payload carries the
response id and bounded metadata — not your prompt and not the generated
text, unless your project has explicitly opted in — so the delivery tells you
*that* it finished and you fetch the content over an authenticated request.

## Cancelling

~~~bash
curl -X POST ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}/cancel \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

Idempotent: cancelling something already finished or already cancelled is not
an error, so a retry after a network failure is safe.

## Retention

A background response keeps its generated text only so that you can fetch it,
and only for your project's retention window — 30 days by default, and
configurable per project in the console. After that the text is pruned and
the metadata remains.

This is the one place the platform stores generated text at all. Everywhere
else, prompts and outputs are not stored: request logs keep metadata only.

## Use an idempotency key

A retried \`POST\` without an [\`Idempotency-Key\`](/docs/idempotency) starts
a second job. With one, the same key and the same body gives you back the
same response id — which is exactly what you want from a client that timed
out and does not know whether the first attempt landed.
`.trim(),
};
