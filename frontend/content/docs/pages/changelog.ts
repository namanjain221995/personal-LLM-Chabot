import type { DocPage } from '../types';
import { EXAMPLE_STATUS } from '../samples';

export const changelog: DocPage = {
  slug: 'changelog',
  title: 'Changelog',
  summary: 'What changed on the developer platform, newest first.',
  section: 'Reference',
  examples: EXAMPLE_STATUS,
  body: `
Dates are the date of the change in this repository. Anything that alters
behaviour on \`/v1\` appears here.

## 2026-09-13 — examples executed

* Every runnable example on these pages was executed against a running
  TechSara stack through the public \`/v1\` edge with a real test key, by
  \`scripts/docs_examples_run.py\`: **48 passed, 0 failed, 0 not run**. The
  36 remaining blocks are JSON response shapes and fragments that are not
  programs on their own, and are listed as such in the run's evidence.
* The run found and fixed two things before this entry was written: the
  public edge did not relay \`WWW-Authenticate\` on a 401 or 403 (it does
  now), and two pages reused one example \`Idempotency-Key\` with different
  bodies, which returns \`409 idempotency_conflict\` to a reader who follows
  both (the background page now uses its own key).
* Signed webhook delivery was checked against the signing code, not end to
  end: the SSRF guard correctly refuses a local receiver, and the run had no
  public HTTPS receiver.

## 2026-09-13 — documentation

* This documentation site published at \`/docs\`: quickstart,
  [authentication](/docs/authentication),
  [API-key security](/docs/key-security), the
  [model reference](/docs/models), [the Responses API](/docs/responses),
  [Chat Completions compatibility](/docs/chat-completions),
  [streaming](/docs/streaming), [background
  responses](/docs/background), [webhooks](/docs/webhooks),
  [errors](/docs/errors), [rate limits](/docs/rate-limits),
  [idempotency](/docs/idempotency), [usage](/docs/usage), worked
  [Python](/docs/python), [JavaScript](/docs/javascript) and
  [cURL](/docs/curl) examples, [tool-calling guidance](/docs/tools),
  [migration notes](/docs/migration), [security best
  practice](/docs/security) and [API status](/docs/status).
* **Examples are marked as not executed.** The eight \`/v1\` routes are
  mounted, but no example on this site has yet been run end to end against a
  running deployment, and each page says so at the top. The examples were
  written from the shipped request and response models, the mounted router,
  the error table, the event grammar and the model registry, and tests hold
  them to that code on every commit. When the end-to-end run passes, an entry
  headed "examples executed" will appear here and the notices will change
  with it.
* **Corrected the same day, against the code**: limits are per project and
  shared by all of its keys; every request that presents a key counts against
  requests per minute, reads included; a limit of \`0\` allows nothing; a
  background response holds a concurrency slot for its whole life; what an
  idempotent replay returns; and the mid-stream failure shapes on both
  streaming dialects.
* Documentation keys (\`tsk_live_0123456789abcdef_…\`) are deliberately
  invalid: the shape is perfect and the checksum is wrong, so pasting one
  gives a clean \`401\` rather than anything that looks like it might work.

## Earlier — the platform being built

The developer platform was built against a written contract: the API-key
format and lifecycle, the scope vocabulary, the V34 schema for projects,
service accounts, keys, responses, idempotency, usage counters and webhook
endpoints, the public request and response models, the error envelope, the
SSE grammar, the model registry, and then the \`/v1\` routes and the developer
console on top of them.

## How to watch for changes

* \`GET /v1/openapi.json\` is the machine-readable surface. Diff it between
  releases.
* [Migration and compatibility](/docs/migration) states what may change
  inside \`/v1\` without notice — new optional fields, new events, new model
  ids — and how to write a client that does not mind.
`.trim(),
};
