import type { DocPage } from '../types';
import { EXAMPLE_STATUS } from '../samples';

// 2026-09-13, owner decision (explicit and final): the public API has NO
// request, token, daily, monthly or concurrency limits. This page used to be
// a table of allowances and three ways to be throttled; a reader who sized a
// client against numbers that are no longer enforced would build a slower
// client for nothing. It now says so first, keeps the technical limits that
// did not go away (context window, output ceiling, body size, the engine's
// shared queue), and describes the enforced mode only as what an operator
// gets by turning PUBLIC_API_ENFORCE_LIMITS on — off by default.
export const rateLimits: DocPage = {
  slug: 'rate-limits',
  title: 'Rate limits',
  summary:
    'There are no usage limits on the API today — what still applies, and ' +
    'how to back off when the model is restarting.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
**The API currently enforces no usage limits.** There is no limit on requests
per minute, no limit on tokens per minute, no daily or monthly token quota and
no cap on how many requests a project runs at once. No \`/v1\` request is
refused with \`rate_limit_error\`, \`quota_exceeded\` or
\`concurrency_limit_exceeded\` for how much it uses, and **no response carries a
\`RateLimit\` or \`RateLimit-Policy\` header** — a header advertising a limit
that does not exist would be worse than none.

That is a decision, not a gap waiting to be filled; the
[changelog](/docs/changelog) records it on 2026-09-13.

## What still applies

These are technical limits of the model and the server, not allowances, and
they apply to every request:

| Limit | Value | What you see |
| --- | --- | --- |
| Body bytes | 1 MiB | \`413 request_too_large\`, checked before the body is parsed. |
| Input tokens | The model's context window | \`400 context_length_exceeded\`, refused before admission. |
| Output tokens | 8,192 by default | The default is clamped to the [model's ceiling](/docs/models); asking for more than the ceiling is a \`400\`. |
| The engine's queue | Shared with the TechSara chat application | A request waits its turn; one that cannot get a place is \`503 model_unavailable\` with \`Retry-After\` — the engine's capacity, not a count of your requests. |

The last row is what "unlimited" does not mean: there is one engine behind
the API, and a thousand requests at once are served as fast as that engine
can serve them, not a thousand times faster. Sending more in parallel than it
can generate makes each answer wait longer — [stream](/docs/streaming) so
your users see output as soon as it exists.

## Usage is still recorded

Every \`/v1\` request that presents a key is still counted, once,
reads included — the console's usage view, the request log and
[\`GET /v1/usage\`](/docs/usage) keep working exactly as before. Recording
what you used is not the same as limiting it. The two routes that take no key
are not counted:
\`GET /v1/openapi.json\` and the browser's \`OPTIONS\` preflight.

## Backing off on 503

The one refusal you should still plan for is the model restarting. While it
does, requests are answered with \`503 model_recovering\` — safe to send
again — or, if the engine is down rather than restarting,
\`503 model_unavailable\`. Both carry \`Retry-After\` in whole seconds, never
less than \`1\`. Treat a \`429 concurrency_limit_exceeded\` from a full
engine queue the same way.

* **Honour \`Retry-After\`.** It is the server's estimate of when the retry
  can succeed; retrying sooner only adds to the queue the restart has to
  drain.
* **Add jitter.** A fleet that retries on the same tick recreates the spike
  it was refused in.
* **Cap your attempts**, and back off harder on \`model_unavailable\` than on
  \`model_recovering\`.
* **Send an [\`Idempotency-Key\`](/docs/idempotency)** on anything you
  retry, so a retry after a lost connection cannot run the work twice.

A streaming request may instead be allowed to wait through a recovery, and
you see a \`response.queued\` event rather than an error — see
[API status](/docs/status). [Errors](/docs/errors#what-to-retry) has a worked
retry loop.

## If an operator enables limits

The limits still exist in the code, behind one server setting,
\`PUBLIC_API_ENFORCE_LIMITS\`, which is **off by default**. Nothing in this
section applies unless the operator of your deployment has turned it on; if
they have, they will tell you.

With it on, limits belong to the **project**: the counters are
summed across all of the project's keys, and a key's own figure can only
*tighten* its project's numbers, never raise them. A limit set to \`0\` allows nothing.
A new project starts with:

| Limit | Default | Enforced |
| --- | --- | --- |
| Requests per minute | 60 | Sliding window, every request that presents a key. |
| Input tokens per minute | 200,000 | Durable counter. |
| Output tokens per minute | 60,000 | Durable counter. |
| Concurrent requests | 4 | In flight at once — a [background response](/docs/background) holds one from its \`202\` until it ends. |
| Tokens per day | 2,000,000 | Resets at midnight UTC. |

Over a limit is \`429\` with \`Retry-After\`: \`rate_limit_error\` for the
per-minute limits, \`quota_exceeded\` for the daily quota and
\`concurrency_limit_exceeded\` for too many in flight — one type,
\`rate_limit_error\`, so one retry branch handles all three. Responses then
carry the IETF draft-11 headers, for example
\`RateLimit: "requests";r=41;t=23\` and
\`RateLimit-Policy: "requests";q=60;w=60, "concurrency";q=4;qu="concurrent-requests"\`.
A \`401\` carries no quota headers at all, deliberately: rate-limit state
handed to a caller who has not proved who they are would let a stranger watch
another tenant's traffic.
`.trim(),
};
