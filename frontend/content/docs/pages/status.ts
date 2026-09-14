import type { DocPage } from '../types';
import { API_BASE_URL, CONSOLE_PATH, DEPLOYS_HELD, EXAMPLE_STATUS } from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): a 503 is never "at capacity"
// any more, and a deploy does not end a generation. Built in either state
// from NO_TIMEOUT_LIVE. 2026-09-14, review: "a connected client does not see a
// deploy" is printed only once a run through the public URL proved it
// (DEPLOYS_HELD, samples.ts); before that a deploy can close the connection.

const INTRO = `

`;
const S_THERE_IS_NO_PUBLIC_STATUS_PAGE = `
## There is no public status page

Plainly, so nobody waits for one: TechSara does not publish a third-party
status page for this API today. What exists instead is below — a cheap live
probe, two error codes that tell you exactly what is wrong, and the console.

If a status page is something your operations team needs, ask for it; it is a
product decision, not a missing switch.
`;
const S_THE_LIVE_PROBE = `
## The live probe

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -i -sS "${API_BASE_URL}/models" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`GET /v1/models\` is the cheapest authenticated call on the platform: it
generates nothing and touches no engine. A \`200\` means the API is up and
your key, service account, project and workspace are all live. A model missing
from the list is not an outage signal: the list shows what this deployment runs
and what your key may use.

Do not treat it as a generation health check. The API answering does not
prove the model is currently serving — that is what the two 503 codes below
are for. And do not poll it every second: a probe is a request, recorded in your
[usage](/docs/usage) like any other.
`;
const S_WHAT_THE_503S_MEAN = `
## What the 503s mean

| Code | Meaning | What to do |
| --- | --- | --- |
| \`model_recovering\` | The engine is restarting. The request is retry-safe. | Wait for \`Retry-After\` and retry. |
| \`model_unavailable\` | The engine is down — or, when the message says so, at capacity: its public queue did not free a place in time. | Back off harder, alert if it persists, and retry with a ceiling. |

Both carry \`Retry-After\` in seconds. Honour it and add jitter — a fleet
that retries on the same tick recreates the spike.

During a recovery a *streaming* request may be allowed to wait, in which case
you will see a \`response.queued\` event rather than an error. That event
exists so you can tell "still queued" from "connection dead" — see
[streaming](/docs/streaming#the-event-sequence).
`;
const S_DEGRADED_NOT_DOWN = `
## Degraded, not down

Two things that are not outages and look like one:

* **A slow answer under load.** The API enforces no usage
  [limits](/docs/rate-limits), so heavy traffic is not refused — it waits
  its turn for the engine it shares with the chat application. The platform
  is healthy; it is busy.
* **A slow first token.** A long prompt takes real time to read before the
  first token appears. [Stream](/docs/streaming), so you can see the
  difference between slow and stuck.
* **A \`503\` "at capacity" from one model while the others answer.** Each
  engine the chat application also uses has its own small public queue, and the
  chat application keeps priority. One busy engine says nothing about the rest —
  see [capacity queues](/docs/rate-limits#capacity-queues-per-engine).
`;
const S_WHERE_TO_LOOK = `
## Where to look

* **The developer console** at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}) — your
  projects' usage, per-key activity and the request log with statuses and
  error codes.
* **\`X-Request-Id\`** on every response. Log it; it is what makes a report
  answerable.
* **Your workspace administrator**, who holds the console and the audit
  trail, and can escalate.
`;
const S_REPORTING_AN_INCIDENT = `
## Reporting an incident

Send the \`X-Request-Id\` values, the times in UTC, the error \`code\` you
received, and what you expected. Never send a key. See
[errors](/docs/errors) for what each code means before you report it — a
\`401\` and a \`503\` are very different conversations.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_WHAT_THE_503S_MEAN = `
## What the 503s mean

| Code | Meaning | What to do |
| --- | --- | --- |
| \`model_recovering\` | The engine is restarting. The request is retry-safe. | Wait for \`Retry-After\` and retry. |
| \`model_unavailable\` | The engine is down — or a physical safeguard of the service tripped: too many open connections, or too little free disk. Never "busy". | Back off harder, alert if it persists, and retry with a ceiling of time, not of attempts. |

Both carry \`Retry-After\` in seconds, at most 60. Honour it and add jitter — a
fleet that retries on the same tick recreates the spike.

Only a **new** request can be refused this way. A request already running when
its engine restarts waits for it and carries on — a stream shows heartbeats, a
Responses stream may show \`response.queued\` — see
[streaming](/docs/streaming#the-event-sequence).
`;
const laterDegradedNotDown = (deploysHeld: boolean): string => `
## Degraded, not down

Things that are not outages and look like one:

* **A slow answer under load.** The API enforces no usage
  [limits](/docs/rate-limits), so heavy traffic is not refused — it waits its
  turn for the engine it shares with the chat application. The platform is
  healthy; it is busy.
* **A slow first token.** A long prompt takes real time to read before the
  first token appears. [Stream](/docs/streaming), so you can see the difference
  between slow and stuck.
${deploysHeld
  ? `* **A pause in a long answer.** A deploy on our side, or a chat turn that needs
  the engine's long-context lane, pauses a running generation, which then
  carries on from where it stopped. Heartbeats keep arriving meanwhile.

**Deploys are not incidents.** A connected client does not see one: the
connection is held while the service restarts. What a client can see is the
network between it and us dropping — [resume](/docs/timeouts#resuming-a-stream),
and it is not an incident either.`
  : `* **A pause in a long answer.** A chat turn that needs the engine's
  long-context lane pauses a running generation, which then carries on from
  where it stopped. Heartbeats keep arriving meanwhile.

**Deploys are not incidents.** A deploy on our side can close a connected
client's connection, but not the generation behind it, which carries on from
where it stopped. [Resume](/docs/timeouts#resuming-a-stream) a stream, or retry
a synchronous call with the same \`Idempotency-Key\`. The network between you
and us dropping is handled the same way, and is not an incident either.`}
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function statusPage({
  noTimeout,
  deploysHeld = DEPLOYS_HELD,
}: {
  noTimeout: boolean;
  deploysHeld?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [INTRO, S_THERE_IS_NO_PUBLIC_STATUS_PAGE, S_THE_LIVE_PROBE, LATER_WHAT_THE_503S_MEAN, laterDegradedNotDown(deploysHeld), S_WHERE_TO_LOOK, S_REPORTING_AN_INCIDENT]
    : [INTRO, S_THERE_IS_NO_PUBLIC_STATUS_PAGE, S_THE_LIVE_PROBE, S_WHAT_THE_503S_MEAN, S_DEGRADED_NOT_DOWN, S_WHERE_TO_LOOK, S_REPORTING_AN_INCIDENT];
  return {
    slug: 'status',
    title: 'API status',
    summary:
      'How to tell whether the API is healthy, what the two 503s mean, and ' +
      'where to look when something is wrong.',
    section: 'Reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const status: DocPage = statusPage({ noTimeout: NO_TIMEOUT_LIVE });
