import type { DocPage } from '../types';
import { API_BASE_URL, CONSOLE_PATH, EXAMPLE_STATUS } from '../samples';

export const status: DocPage = {
  slug: 'status',
  title: 'API status',
  summary:
    'How to tell whether the API is healthy, what the two 503s mean, and ' +
    'where to look when something is wrong.',
  section: 'Reference',
  examples: EXAMPLE_STATUS,
  body: `
## There is no public status page

Plainly, so nobody waits for one: TechSara does not publish a third-party
status page for this API today. What exists instead is below — a cheap live
probe, two error codes that tell you exactly what is wrong, and the console.

If a status page is something your operations team needs, ask for it; it is a
product decision, not a missing switch.

## The live probe

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -i -sS "${API_BASE_URL}/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`GET /v1/models\` is the cheapest authenticated call on the platform: it
generates nothing and touches no engine. A \`200\` means the API is up and
your key, service account, project and workspace are all live.

Do not treat it as a generation health check. The API answering does not
prove the model is currently serving — that is what the two 503 codes below
are for. And do not poll it every second: a probe is a request and counts
against your [rate limits](/docs/rate-limits).

## What the 503s mean

| Code | Meaning | What to do |
| --- | --- | --- |
| \`model_recovering\` | The engine is restarting. The request is retry-safe. | Wait for \`Retry-After\` and retry. |
| \`model_unavailable\` | The engine is down. | Back off harder, alert, and retry with a ceiling. |

Both carry \`Retry-After\` in seconds. Honour it and add jitter — a fleet
that retries on the same tick recreates the spike.

During a recovery a *streaming* request may be allowed to wait, in which case
you will see a \`response.queued\` event rather than an error. That event
exists so you can tell "still queued" from "connection dead" — see
[streaming](/docs/streaming#the-event-sequence).

## Degraded, not down

Two things that are not outages and look like one:

* **\`429\` under load.** Your project reached a limit. The platform is
  healthy; your concurrency is not. See [rate limits](/docs/rate-limits).
* **A slow first token.** A long prompt takes real time to read before the
  first token appears. [Stream](/docs/streaming), so you can see the
  difference between slow and stuck.

## Where to look

* **The developer console** at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}) — your
  projects' usage, per-key activity and the request log with statuses and
  error codes.
* **\`X-Request-Id\`** on every response. Log it; it is what makes a report
  answerable.
* **Your workspace administrator**, who holds the console and the audit
  trail, and can escalate.

## Reporting an incident

Send the \`X-Request-Id\` values, the times in UTC, the error \`code\` you
received, and what you expected. Never send a key. See
[errors](/docs/errors) for what each code means before you report it — a
\`401\` and a \`503\` are very different conversations.
`.trim(),
};
