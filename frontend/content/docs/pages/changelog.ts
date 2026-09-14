import type { DocPage } from '../types';
import {
  DEPLOYS_HELD,
  EXAMPLE_STATUS,
  LONG_OUTPUT_WALL_CLOCK_LIVE,
  WALL_CLOCK_PENDING_NOTE,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): the entry for the release is
// listed only once the release is live (NO_TIMEOUT_LIVE), newest first — a
// changelog entry for a change the running API does not have would be the
// stale claim this site's switches exist to prevent.

const INTRO = `
Dates are the date of the change in this repository. Anything that alters
behaviour on \`/v1\` appears here.
`;
const S_2026_09_13_EVERY_MODEL_ON_THE_API_AND_ANSWERS_UP_TO_1_000_00 = `
## 2026-09-13 — every model on the API, and answers up to 1,000,000 tokens

* **Six models instead of one.** \`/v1\` now offers every model TechSara runs:
  \`techsara-35b\` (the chat model), \`techsara-8b-vision\` (a smaller
  vision-language model), \`techsara-ocr\` (reads text out of an image),
  \`techsara-embed\` (embeddings), \`techsara-rerank\` (reranking) and
  \`techsara-whisper\` (speech to text). \`GET /v1/models\` lists the ones this
  deployment runs and your key may use. See the [model reference](/docs/models).
* **Three new endpoints, three new scopes.** [\`POST /v1/embeddings\`](/docs/embeddings)
  (\`embeddings.write\`), [\`POST /v1/rerank\`](/docs/rerank) (\`rerank.write\`)
  and [\`POST /v1/audio/transcriptions\`](/docs/audio-transcriptions)
  (\`audio.write\`). New keys get all three by default. **Keys created before
  this change do not have them** and answer \`403 insufficient_scope\` on those
  endpoints; create a new key.
* **Existing projects can reach the new chat models.** A project whose model
  allowlist is empty — the default — can now use \`techsara-8b-vision\` and
  \`techsara-ocr\` with its existing \`responses.write\` keys, and every other
  new model with a key that holds the matching scope. Set an allowlist if that is
  not what you want.
* **Images on the two generation endpoints.** A \`user\` message may carry image
  parts, as \`data:\` URLs only — a link is refused, never fetched. The body limit
  on \`/v1/responses\` and \`/v1/chat/completions\` is 20 MiB; all the text in it is
  still limited to 1 MiB. See [images and OCR](/docs/images).
* **\`max_output_tokens\` up to 1,000,000 on \`techsara-35b\`.** Input and output
  share the context window, so a value within the model's ceiling that is more
  than the prompt leaves is now **clamped** instead of refused; above the ceiling
  is still a \`400\`. Every response object gains \`max_output_tokens\` (the ceiling
  applied) and \`incomplete_details\` (set when the answer reached it), and
  \`chat.completion\` gains \`max_output_tokens\`. Chat Completions accepts
  \`max_completion_tokens\` as an alias of \`max_tokens\`. The default when you
  send nothing is still 8,192. See [long outputs](/docs/long-output), which also
  explains why a long answer needs streaming or background.
* **The wall clock follows the request.** A generation's time limit is now sized to
  its \`max_output_tokens\`, up to six hours, and a generation that reaches it
  keeps the text it had written.
${LONG_OUTPUT_WALL_CLOCK_LIVE ? `* **The per-request wall clock is live.** A \`techsara-35b\` generation is no longer
  stopped at the chat application's 70-minute clock: it runs to its own, so a
  1,000,000-token answer can finish.
` : `* ${WALL_CLOCK_PENDING_NOTE}\n`}* **Capacity queues per engine.** The engines are shared with the TechSara chat
  application, which keeps priority. Public requests to each wait in a small
  queue; one that cannot start in time is \`503 model_unavailable\` with
  \`Retry-After\` — capacity shared by every caller, never a \`429\` and never a
  limit on your key. A background response waits in \`queued\` for up to an hour.
  See [rate limits](/docs/rate-limits#capacity-queues-per-engine).
* **What did not change**: no usage limits, the error codes (every refusal above
  uses a code that already existed), the event names, the 8,192 default, the
  webhook events, and usage recorded once per request. \`Idempotency-Key\` is
  still accepted on \`/v1/responses\` and \`/v1/chat/completions\`, and refused on
  the three new endpoints.
* **Examples are still marked as not executed.** The new pages and examples have
  not been run against a running deployment yet; the notice on every page says
  so.
`;
const S_2026_09_13_USAGE_LIMITS_REMOVED = `
## 2026-09-13 — usage limits removed

* **The API no longer enforces any usage limit, by decision.** There is no
  limit on requests per minute or tokens per minute, no daily or monthly
  token quota and no per-project concurrency cap. No \`/v1\` request is
  refused with \`rate_limit_error\`, \`quota_exceeded\` or
  \`concurrency_limit_exceeded\` for volume, and no response carries a
  \`RateLimit\` or \`RateLimit-Policy\` header any more.
* **Two refusals that are not limits changed code, so that no \`429\` is
  sent at all.** The engine's shared queue being full is now
  \`503 model_unavailable\` ("at capacity", retry-safe, with
  \`Retry-After\`) instead of \`429 concurrency_limit_exceeded\`. Repeating
  an \`Idempotency-Key\` whose first request is still running is now
  \`409 idempotency_conflict\` with \`Retry-After\` instead of
  \`429 rate_limit_error\`; a \`409\` without \`Retry-After\` is still
  the different-body conflict and is not retryable. The retry samples on the
  [errors](/docs/errors), [Python](/docs/python) and
  [JavaScript](/docs/javascript) pages handle both.
* **What did not change**: the model's context window
  (\`400 context_length_exceeded\`), the \`max_output_tokens\` ceiling, the
  1 MiB body cap (\`413\`), the engine's shared queue, and
  \`503 model_recovering\` with \`Retry-After\` while the model restarts.
  Usage is still recorded once per request, so the console, the request log
  and \`GET /v1/usage\` read exactly as before.
* The limits remain in the code behind one operator setting,
  \`PUBLIC_API_ENFORCE_LIMITS\`, off by default. See
  [rate limits](/docs/rate-limits).
* **Examples are marked as not executed again.** The pages changed with the
  decision — the \`curl -i\` sample no longer shows \`RateLimit\` headers —
  so the run recorded below no longer covers what the pages say. The notice
  on every page changes back when the examples have been run again.
`;
const S_2026_09_13_THE_FIRST_END_TO_END_RUN_OF_THE_EXAMPLES = `
## 2026-09-13 — the first end-to-end run of the examples

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
`;
const S_2026_09_13_DOCUMENTATION = `
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
  streaming dialects. (The limits in this bullet apply only where an operator
  enables them since the removal recorded above.)
* Documentation keys (\`tsk_live_0123456789abcdef_…\`) are deliberately
  invalid: the shape is perfect and the checksum is wrong, so pasting one
  gives a clean \`401\` rather than anything that looks like it might work.
`;
const S_EARLIER_THE_PLATFORM_BEING_BUILT = `
## Earlier — the platform being built

The developer platform was built against a written contract: the API-key
format and lifecycle, the scope vocabulary, the V34 schema for projects,
service accounts, keys, responses, idempotency, usage counters and webhook
endpoints, the public request and response models, the error envelope, the
SSE grammar, the model registry, and then the \`/v1\` routes and the developer
console on top of them.
`;
const S_HOW_TO_WATCH_FOR_CHANGES = `
## How to watch for changes

* \`GET /v1/openapi.json\` is the machine-readable surface. Diff it between
  releases.
* [Migration and compatibility](/docs/migration) states what may change
  inside \`/v1\` without notice — new optional fields, new events, new model
  ids — and how to write a client that does not mind.
`;

// ------------------------------------------------ after the no-timeout release --

// 2026-09-14, review: the heading and every bullet stay true whether or not a
// run through the public URL has proved deploys are held (DEPLOYS_HELD,
// samples.ts); only the sentences about a connected client depend on it. The
// heading does not, so its anchor never moves when the probe is recorded.
const laterPre0 = (deploysHeld: boolean): string => `
## 2026-09-13 — no timeouts, resumable streams, and generations that outlive our deploys

* **There are no server timeouts on \`/v1\`.** No request is ended because it has
  run for a long time: the per-generation wall clock is gone, and so is the
  30-second wait for capacity. After authentication a response sends its first
  byte within 15 seconds and another at least every 15 seconds — a \`: ping\` on
  a stream, a space before a synchronous JSON body — so no proxy between you and
  the API closes it. See [timeouts](/docs/timeouts).
* **A synchronous call that runs past 15 seconds gets its \`200\` early.** The
  JSON follows after leading spaces, and a failure after that point is in the
  body: \`"status": "failed"\` on \`/v1/responses\`, \`"choices": []\` with an
  \`error\` on \`/v1/chat/completions\`. Check the body, not only the status.
  A \`response_format=text\` transcript can start with those spaces: strip it.
* **Busy is never an error.** A request that has to wait for an engine waits —
  \`response.queued\` and heartbeats, \`: queued\` comments, or spaces — however
  long it takes. \`503\` now means only an engine that is down or restarting, or a
  physical safeguard of the service, with a \`Retry-After\` of 60 seconds or less.
${deploysHeld
  ? `* **Deploys no longer interrupt you.** Generations are written ahead as they run
  and continue after a restart of the service from where they stopped, and a
  connected client is held while the service restarts. Background jobs no longer
  fail with "The service restarted while this response was running."`
  : `* **Deploys no longer end your work.** Generations are written ahead as they run
  and continue after a restart of the service from where they stopped. A deploy
  on our side can still close a connected client's connection: resuming the
  stream, or retrying with the same \`Idempotency-Key\`, picks the generation up.
  Background jobs no longer fail with "The service restarted while this response
  was running."`}
* **Streams can be resumed.** \`GET /v1/responses/{id}?stream=true&starting_after=N\`
  replays a response's events after sequence number \`N\` and follows it live —
  for the key that created it, while it runs and for one hour after. On Chat
  Completions, a retry with the same \`Idempotency-Key\` replays the stream.
* **Retries join the request instead of repeating it.** The same
  \`Idempotency-Key\` and body attaches to a running request rather than
  answering \`409\`; a \`409\` now means a different body or a different
  credential, with \`x-should-retry: false\`. The \`openai\` packages' own retries
  of an identical request attach even without a key.
* **New on the wire**: \`stream\` together with \`background\`; \`store\` on both
  generation endpoints (\`false\` opts out of being written down, and so out of
  resuming); the \`response.output_item.added\`, \`response.content_part.added\`,
  \`response.content_part.done\` and \`response.output_item.done\` events; and
  the \`x-should-retry\` header.
* **Larger requests where the size is only shape**: embeddings take up to 2,048
  inputs and rerank up to 1,000 documents, in a body of up to 8 MiB.
  Transcription has no duration limit, takes up to 89 MiB of audio in one request,
  and accepts \`stream\`.
* **What still ends a request** — the engine proven down for 30 minutes, three
  attempts without progress, the same request in two engine crashes, a revoked
  key, ${deploysHeld ? 'our service unreachable for 30 minutes' : 'our service unreachable for longer than your client retries'}, or a client that disconnects and
  does not return — is listed in [timeouts](/docs/timeouts#what-still-ends-a-request).
* **Client settings.** Python: \`timeout=Timeout(None, connect=10.0)\` — never
  \`0\`. Node: \`timeout: 2_147_483_647\` — never \`0\` or \`Infinity\`, and note
  that from version 7.5 it covers a whole non-streamed call. Both: an
  \`Idempotency-Key\` per generation call.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function changelogPage({
  noTimeout,
  deploysHeld = DEPLOYS_HELD,
}: {
  noTimeout: boolean;
  deploysHeld?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [INTRO, laterPre0(deploysHeld), S_2026_09_13_EVERY_MODEL_ON_THE_API_AND_ANSWERS_UP_TO_1_000_00, S_2026_09_13_USAGE_LIMITS_REMOVED, S_2026_09_13_THE_FIRST_END_TO_END_RUN_OF_THE_EXAMPLES, S_2026_09_13_DOCUMENTATION, S_EARLIER_THE_PLATFORM_BEING_BUILT, S_HOW_TO_WATCH_FOR_CHANGES]
    : [INTRO, S_2026_09_13_EVERY_MODEL_ON_THE_API_AND_ANSWERS_UP_TO_1_000_00, S_2026_09_13_USAGE_LIMITS_REMOVED, S_2026_09_13_THE_FIRST_END_TO_END_RUN_OF_THE_EXAMPLES, S_2026_09_13_DOCUMENTATION, S_EARLIER_THE_PLATFORM_BEING_BUILT, S_HOW_TO_WATCH_FOR_CHANGES];
  return {
    slug: 'changelog',
    title: 'Changelog',
    summary: 'What changed on the developer platform, newest first.',
    section: 'Reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const changelog: DocPage = changelogPage({ noTimeout: NO_TIMEOUT_LIVE });
