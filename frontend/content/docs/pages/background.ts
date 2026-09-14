import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): background jobs queue as rows
// and wait for capacity with no clock, survive a deploy by resuming from their
// write-ahead event log, and may be streamed and resumed. Built in either
// state from NO_TIMEOUT_LIVE.

const INTRO = `
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
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": null
}
~~~

The \`202\` is sent **after the row is durable and before any expensive
work**. That ordering is the promise: if you hold an id, the work exists and
will survive your disconnect — and a crash between "accepted" and "started"
cannot lose a job you were told we had.

\`max_output_tokens\` in the \`202\` is the ceiling the server planned from
your request, already clamped to what your prompt leaves in the context window;
the finished response carries the exact value it applied. See
[long outputs](/docs/long-output).

\`stream\` and \`background\` cannot both be true. There is no stream to
attach to.

Background work is offered on the three chat models — \`${MODEL_ID}\`,
\`techsara-8b-vision\` and \`techsara-ocr\`. Embeddings, reranking and
transcription are synchronous only.

There is no cap on how many background responses a project runs at once —
the API enforces no concurrency [limits](/docs/rate-limits). They share the
engines with everything else, so a thousand submitted together are accepted
together and then served as fast as the engines can serve them. A job that
cannot get a place in its engine's queue ends \`failed\` with
\`model_unavailable\` — the engine's capacity, not a limit on your
project — and can be submitted again.
`;
const S_WAITING_FOR_CAPACITY = `
## Waiting for capacity

Some engines have a small public queue in front of them, because the TechSara
chat application uses them too and keeps priority: \`techsara-8b-vision\`,
\`techsara-ocr\`, and \`${MODEL_ID}\` for a long generation (\`max_output_tokens\`
of more than 8,192 tokens). A background job on
one of them **waits in \`queued\` for its turn — for up to an hour** — rather
than failing the moment the queue is full. A synchronous or streaming request
waits only about 30 seconds before it is refused with \`503\`, which is one more
reason to send long work in the background.

A job that is still waiting after an hour ends \`failed\` with
\`model_unavailable\`. Cancelling a job while it is \`queued\` stops it before
it starts.
`;
const S_LONG_JOBS_AND_RESTARTS = `
## Long jobs and restarts

A background response runs until it finishes, fails, is cancelled or reaches
its wall clock — up to six hours for the longest answers. If it reaches the
wall clock it ends \`failed\` with \`timeout\`, and **the text it had written
is kept** in its \`output\`.

**A service restart ends a running job.** When the service restarts — which is
what deploying a new version does — a job that was \`queued\` or
\`in_progress\` ends \`failed\` with \`model_unavailable\` and the message
"The service restarted while this response was running." The failure is safe to
retry: submit the job again **with a new \`Idempotency-Key\`** — the old key
still names the failed job, and a request that repeats it is handed that job
back rather than a new one. The longer a job runs, the likelier it is to meet a
restart, so a client that submits hours-long work should treat this failure as
routine and resubmit.
`;
const S_COLLECTING_THE_ANSWER = `
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

Poll politely — a few seconds between attempts for a short job, a minute for
one measured in hours, always with a ceiling — and
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
`;
const S_CANCELLING = `
## Cancelling

~~~bash
curl -X POST ${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}/cancel \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

Idempotent: cancelling something already finished or already cancelled is not
an error, so a retry after a network failure is safe.
`;
const S_RETENTION = `
## Retention

A background response keeps its generated text only so that you can fetch it,
and only for your project's retention window — 30 days by default, and
configurable per project in the console. After that the text is pruned and
the metadata remains.

This is the one place the platform stores generated text at all. Everywhere
else, prompts and outputs are not stored: request logs keep metadata only.
`;
const S_USE_AN_IDEMPOTENCY_KEY = `
## Use an idempotency key

A retried \`POST\` without an [\`Idempotency-Key\`](/docs/idempotency) starts
a second job. With one, the same key and the same body gives you back the
same response id — which is exactly what you want from a client that timed
out and does not know whether the first attempt landed.
`;

// ------------------------------------------------ after the no-timeout release --

const INTRO_LATER = `
Some work outlives the request that asked for it: a long document, a
serverless function with a hard ceiling, a mobile client on a bad connection.
Send \`"background": true\` and the platform takes the job.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: aaaa-bbbb-cccc-dddd" \\
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
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": null
}
~~~

The \`202\` is sent **after the row is durable and before any expensive
work**. That ordering is the promise: if you hold an id, the work exists and
will survive your disconnect — and a crash between "accepted" and "started"
cannot lose a job you were told we had.

\`max_output_tokens\` in the \`202\` is the ceiling the server planned from
your request, already clamped to what your prompt leaves in the context window;
the finished response carries the exact value it applied. See
[long outputs](/docs/long-output).

\`stream\` and \`background\` can both be true: the job is durable, and you
watch it as it is written. If the connection drops, the job carries on, and you
[resume the stream](/docs/timeouts#resuming-a-stream) where you left it.

Background work is offered on the three chat models — \`${MODEL_ID}\`,
\`techsara-8b-vision\` and \`techsara-ocr\`. Embeddings, reranking and
transcription are synchronous only.

There is no cap on how many background responses a project runs at once —
the API enforces no concurrency [limits](/docs/rate-limits). They share the
engines with everything else, so a thousand submitted together are accepted
together and then served as fast as the engines can serve them, in the order
they arrived.
`;
const LATER_WAITING_FOR_CAPACITY = `
## Waiting for capacity

Every engine is shared with the TechSara chat application, which keeps
priority, and each has a small public queue in front of it. A background job
**waits in \`queued\` for its turn, however long that takes**: no clock turns
the wait into a failure, and a queued job holds nothing but its row while it
waits. Synchronous and streaming requests wait the same way, with the
connection kept alive (see [timeouts](/docs/timeouts#waiting-for-capacity)).

Cancelling a job while it is \`queued\` stops it before it starts.
`;
const LATER_LONG_JOBS_AND_RESTARTS = `
## Long jobs and restarts

A background response runs until it finishes, fails, is cancelled or reaches
its \`max_output_tokens\`. **There is no wall clock**: a million-token answer
that takes four hours is simply a four-hour job.

**A service restart does not end it.** Everything the job generates is written
down as it goes. When the service restarts — which is what deploying a new
version does — the job pauses, and afterwards carries on from where it
stopped: the text so far is kept and the model continues it. The only cost is
time: the model has to read the prompt and the text so far again before it
writes the next token. Jobs resume one at a time, oldest first.

A chat turn in the TechSara application that needs the engine's long-context
lane can pause a very long job the same way, for as long as that turn takes. It
resumes by itself, and the text is unchanged.

What does end a running job: the engine proven down for 30 minutes without a
break; three attempts in a row that make no progress while the engine is
otherwise serving; the job being caught up in two engine crashes (it then fails
with \`x-should-retry: false\`); or its key being revoked. See
[what still ends a request](/docs/timeouts#what-still-ends-a-request).
`;
const LATER_COLLECTING_THE_ANSWER = `
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

Poll politely — a few seconds between attempts for a short job, a minute for
one measured in hours, always with a ceiling — and
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

### Or watch it

The key that created the job can follow it as a stream at any time — from the
start, or after the last event it saw:

~~~bash
curl -N "${API_BASE_URL}/responses/${EXAMPLE_RESPONSE_ID}?stream=true&starting_after=0" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

See [resuming a stream](/docs/timeouts#resuming-a-stream).

### Or be told

A [webhook](/docs/webhooks) endpoint on your project turns the poll loop into
a delivery: \`response.completed\`, \`response.failed\` and
\`response.cancelled\` are the subscribable events, sent once, when the job
really ends — not when it pauses for a restart. The payload carries the
response id and bounded metadata — not your prompt and not the generated
text, unless your project has explicitly opted in — so the delivery tells you
*that* it finished and you fetch the content over an authenticated request.
`;
const LATER_RETENTION = `
## Retention

A background response keeps its generated text so that you can fetch it, for
your project's retention window — 30 days by default, and configurable per
project in the console. After that the text is pruned and the metadata remains.

While any response runs, background or not, its request is kept so that it can
be resumed, and its stream events are kept until one hour after it ends; both
are then deleted. Only the key that created it, or another key of its service
account, can read those events back. Send \`"store": false\` on a request that
must keep nothing — it then cannot be resumed.
`;
const LATER_USE_AN_IDEMPOTENCY_KEY = `
## Use an idempotency key

A retried \`POST\` without an [\`Idempotency-Key\`](/docs/idempotency) may start
a second job. With one, the same key and the same body gives you back the same
response id — which is exactly what you want from a client that timed out and
does not know whether the first attempt landed.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function backgroundPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO_LATER, LATER_WAITING_FOR_CAPACITY, LATER_LONG_JOBS_AND_RESTARTS, LATER_COLLECTING_THE_ANSWER, S_CANCELLING, LATER_RETENTION, LATER_USE_AN_IDEMPOTENCY_KEY]
    : [INTRO, S_WAITING_FOR_CAPACITY, S_LONG_JOBS_AND_RESTARTS, S_COLLECTING_THE_ANSWER, S_CANCELLING, S_RETENTION, S_USE_AN_IDEMPOTENCY_KEY];
  return {
    slug: 'background',
    title: 'Background responses',
    summary:
      'Hand the work over, get an id back immediately, and collect the answer ' +
      'by polling or by webhook.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const background: DocPage = backgroundPage({ noTimeout: NO_TIMEOUT_LIVE });
