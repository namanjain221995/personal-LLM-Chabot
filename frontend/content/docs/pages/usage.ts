import type { DocPage } from '../types';
import { API_BASE_URL, CONSOLE_PATH, EXAMPLE_STATUS } from '../samples';

export const usage: DocPage = {
  slug: 'usage',
  title: 'Usage reporting',
  summary:
    'What is counted, when it is written, and where to read your project ' +
    'counters back.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
## Reading your counters

~~~http
GET /v1/usage
~~~

Requires the \`usage.read\` scope, and is recorded like every other call —
recorded, not limited: the API enforces no usage
[limits](/docs/rate-limits). The range is project-scoped and bounded —
you read your own project's counters over a window, not the workspace's
history in one call.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl "${API_BASE_URL}/usage?start_date=2026-09-01&end_date=2026-09-13" \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

| Parameter | Rule |
| --- | --- |
| \`start_date\` | ISO \`YYYY-MM-DD\`. Defaults to the start of a 30-day window ending at \`end_date\` — 29 days before it, because both ends count. |
| \`end_date\` | ISO \`YYYY-MM-DD\`. Defaults to today. |

The range is inclusive at both ends, must not run backwards, and may cover at
most **93 days** counting both ends — \`2026-06-01\` to \`2026-09-01\` is
exactly 93 — and anything longer is a \`400\` naming \`start_date\`. A
malformed date is a \`400\` too, rather than a silent fall back to a window
you did not ask for.

~~~json
{
  "object": "list",
  "start_date": "2026-09-01",
  "end_date": "2026-09-13",
  "data": [
    {
      "date": "2026-09-01",
      "requests": 412,
      "input_tokens": 183204,
      "output_tokens": 52117,
      "errors": 3,
      "rate_limited": 0
    }
  ]
}
~~~

One row per day, for your project. \`errors\` counts requests that ended in a
failure; \`rate_limited\` counts the ones refused for a usage limit, and
stays \`0\` unless an operator has
[enabled limits](/docs/rate-limits#if-an-operator-enables-limits).

The same shape is in \`/v1/openapi.json\` if you are generating a client.

## What gets counted

One row is written per request, once, when it finishes — never per token.
Each row carries:

* the route and the model;
* input and output tokens;
* time to first token and total duration;
* the final status and, on a failure, the error code;
* the project, the key and the request id.

Prompts and generated text are **not** part of it. Request logs keep metadata
only; the one exception is a [background response](/docs/background), whose
output is retained for your project's retention window so you can fetch it.

## \`null\` is not zero

\`usage\` is \`null\` when the engine reported no counts. It is never \`0\`
for "we did not measure": a zero would be a lie in the direction of an
under-charge, and a counter that quietly reads zero is worse than one that
admits it does not know.

Your aggregation code should therefore treat a missing count as **unknown**
and not as nothing. Summing \`null\` as \`0\` is the classic way to build a
dashboard that is confidently wrong.

## Where else to look

* **The developer console** at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}) shows
  your projects' usage, the per-key activity and the request log.

API traffic goes into the same ledger the rest of the product uses, so it
shows up in the workspace's existing analytics rather than in a parallel
universe of its own.

## Timing

Counters are written when a request completes, so a request still in flight
is not in them yet, and a very recent request may be seconds behind.
`.trim(),
};
