import type { DocPage } from '../types';
import { EXAMPLE_STATUS } from '../samples';

export const rateLimits: DocPage = {
  slug: 'rate-limits',
  title: 'Rate limits',
  summary:
    'What a project is allowed per minute, per day and at once — and how to ' +
    'back off when you reach it.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
Limits belong to the **project**. Every key in a project draws on the same
allowance: the counters are summed across all of the project's keys, so
minting a second key does not buy a second allowance. A key can carry its own
lower requests-per-minute or concurrency figure to keep one service inside a
share of the project — it can only ever *tighten* its project's numbers,
never raise them. A key with no figure of its own uses the project's.

| Limit | Default | Enforced |
| --- | --- | --- |
| Requests per minute | 60 | Sliding window, durable across restarts. |
| Input tokens per minute | 200,000 | Durable counter. |
| Output tokens per minute | 60,000 | Durable counter. |
| Concurrent requests | 4 | In flight at once, across sync, streaming and background. |
| Tokens per day | 2,000,000 | Durable, survives a restart. |
| Body bytes | 1 MiB | Checked before parsing. |
| Input tokens | The model's ceiling | Checked before admission. |
| Output tokens | 8,192 by default | Clamped to the model's ceiling. |

Your project's real numbers are in the console; the defaults above are what a
new project starts with.

## Every request counts

Every \`/v1\` request that presents a key counts one against requests per
minute — reads included. \`GET /v1/models\`, \`GET /v1/responses/{id}\`,
\`POST /v1/responses/{id}/cancel\` and \`GET /v1/usage\` spend the same
allowance a generation does, so a status poll in a tight loop is a rate limit
you are choosing to hit. Only the two routes that take no key are free:
\`GET /v1/openapi.json\` and the browser's \`OPTIONS\` preflight.

A request refused *by* a limit does not use up the allowance it was refused
for.

## Zero means zero

A limit set to \`0\` allows nothing. A project whose requests per minute is
\`0\` answers every request with \`429\`; a daily token quota of \`0\` is
\`quota_exceeded\` on the first request; a concurrency of \`0\` refuses every
request as \`concurrency_limit_exceeded\`. That is how an administrator pauses
a project without revoking its keys, and a key whose own figure is \`0\` is
parked the same way while its siblings carry on. \`0\` is never read as "use
the default" — only a key figure that was never set inherits the project's.

## Concurrency

One count per project covers every kind of request: a synchronous call holds
a slot until its answer is written, a stream holds one until the stream ends,
and a [background response](/docs/background) holds one from its \`202\`
until it completes, fails or is cancelled. Over the limit is an immediate
\`429 concurrency_limit_exceeded\` — never a queue, so the wait is yours to
decide on rather than hidden inside a request you are timing.

## Tokens

Input tokens are estimated, pessimistically, and that estimate is reserved
against the per-minute and daily counters when the request is admitted; when
it finishes, the estimate is replaced by the counts the engine reported,
written once. Output
cannot be known in advance, so the output-per-minute limit refuses the request
*after* the one that crossed the line. The daily quota counts input and output
together.

The daily quota resets at **midnight UTC**, not at midnight where you are. A
caller in Kolkata gets the reset at 05:30 local.

## The headers

Every authenticated \`/v1\` response carries, \`429\`s included:

~~~http
RateLimit: "requests";r=41;t=23
RateLimit-Policy: "requests";q=60;w=60, "concurrency";q=4;qu="concurrent-requests"
~~~

\`r\` is how many requests remain in the current window and \`t\` is the
seconds until it resets; \`q\` and \`w\` are the quota and window that apply to
the key you sent. Read them and slow down *before* you are refused — a client
that only reacts to \`429\` spends part of every minute being refused.

The fields follow the IETF \`RateLimit\` header draft (revision 11): two
structured fields, not the older \`RateLimit-Limit\` / \`-Remaining\` /
\`-Reset\` triple some libraries still expect. The token limits are enforced
but not advertised in these fields — the draft has no unit for model tokens,
and a number in the wrong unit would be worse than none.

A \`401\` carries no quota headers at all, deliberately: rate-limit state
handed to a caller who has not proved who they are would let a stranger watch
another tenant's traffic.

Every \`429\` and every \`503\` carries \`Retry-After\`, in whole seconds, at
least \`1\`, with jitter already added so a fleet throttled together does not
return together.

If you are calling from a browser, these headers plus \`X-Request-Id\` and
\`Retry-After\` are exposed to your JavaScript on cross-origin responses —
without that a browser client can read the status code and nothing else.

## The three ways to be throttled

| Code | Meaning | What to do |
| --- | --- | --- |
| \`rate_limit_error\` | Requests or tokens per minute exceeded. | Wait out the window. |
| \`quota_exceeded\` | The daily token quota is exhausted. | Wait for midnight UTC, or ask for more quota. |
| \`concurrency_limit_exceeded\` | Too many requests in flight. | Run fewer at once — a queue of 4 that never blocks beats 40 that mostly fail. |

All three share the \`rate_limit_error\` type, so one retry branch handles
them: honour \`Retry-After\`, add jitter, cap the attempts. See
[errors](/docs/errors#what-to-retry) for a worked retry loop.

## Why the quota gate comes first

Generation lanes are shared with the TechSara chat application, and the quota
is checked *before* a request is admitted to one. Without that ordering a
single project could hold every lane and starve everyone signed in to the
product. It is also why \`concurrency_limit_exceeded\` is a normal thing to see
under load rather than a fault: the limit is doing its job.

## Designing for the limits

* **Batch nothing you can stream.** A streamed answer holds one slot and
  gives your user output immediately.
* **Use [background responses](/docs/background)** for long work and let the
  webhook wake you, instead of polling — every poll is a request.
* **Poll politely** when you must: seconds apart, with a ceiling.
* **Add jitter.** A fleet that retries on the same tick recreates the spike
  it was throttled for.
* **Set \`max_output_tokens\`.** The token-per-minute counters care about
  what you actually generate.
* **Separate the workloads by project**, not by key. Two keys in one project
  share one allowance; a batch job and an interactive feature in different
  projects cannot exhaust each other.

## Asking for more

Limits are set per project in the console; raising a workspace's ceiling is
super-admin territory. Bring the numbers: the requests per minute you need,
your typical prompt and answer sizes, and whether the traffic is interactive
or batch.
`.trim(),
};
