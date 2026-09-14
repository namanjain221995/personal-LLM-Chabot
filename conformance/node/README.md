# openai-node conformance suite for the TechSara `/v1` API

This suite runs the official OpenAI JavaScript SDK against a TechSara `/v1` base
URL with a test key. It checks that what the SDK does matches
`docs/developer-platform/CONTRACT.md` (CONTRACT-3). It is the Node counterpart of
`conformance/python`, and both suites cover the same list of features.

* SDK: `openai` **7.15.0** and `undici` **7.29.1**, pinned exactly in
  `package.json` and locked in `package-lock.json`.
* Runner: `node:test`. There are no other dependencies.
* Node.js **22 or newer**. openai-node 7.x declares `engines.node >= 22`, and
  `scripts/check-node.mjs` refuses to run on anything older.

## Outcomes

Each test ends in one of four states. The table reporter writes them to
`results/table.md`, with the base URL the run used and the totals first.

| outcome | meaning |
|---|---|
| PASS | The stack behaves as the contract says. |
| FAIL | A contract or SDK-compatibility defect. **The run exits non-zero.** |
| XFAIL | A *planned* feature that is not on this stack yet. The test ran, failed, and the reason is recorded. |
| SKIP | Not run, or not able to prove anything, with the reason: no base URL; an opt-in test; a condition the suite must not create; a key that lacks a scope the stack has (re-provision); a background cancel that lost the race to completion; the long-request capacity slot busy. |

**Planned features are strict expected failures.** You can see them in
`lib/feature-list.mjs` (the data) and `lib/features.mjs` (the wrapper). Every
test of a planned feature still runs on every stack:

* If it fails, it is reported as XFAIL.
* If it **passes** while the feature is still marked `planned`, it is reported as
  `FAIL: XPASS(strict)`. That tells you to set the feature's `status` to
  `built`, so from then on the test guards it.

A test that passes against a 404 therefore cannot hide.

To promote features for a single run without editing the file:

```sh
CONFORMANCE_BUILT_FEATURES=embeddings,rerank npm test
```

| feature id | what it covers | scopes the tests need | source |
|---|---|---|---|
| `six-models` | all six public models listed; `techsara-8b-vision` and `techsara-ocr` on `/v1/responses`; the model-kind 400 | `models.read`, `responses.write` | CONTRACT-3 §7, §15 |
| `one-million-output` | output ceiling min(1,000,000, context window) on `techsara-35b`; `max_output_tokens` and `incomplete_details` on every response object | `models.read`, `responses.write` | §8.3, §9 |
| `max-completion-tokens` | `max_completion_tokens` as the alias of `max_tokens`; sending both is 400 | `responses.write` | §8.2 |
| `image-input` | `input_image` and `image_url` data URLs; an `http` URL refused by the data:-only rule | `responses.write` | §8.1, §8.2 |
| `embeddings` | `POST /v1/embeddings` (float and the SDK default base64) | `embeddings.write` | §8.4 |
| `rerank` | `POST /v1/rerank` (raw HTTP, because the SDK has no method for it); id `rrk_<24 hex>` | `rerank.write` | §8.5, §16 |
| `audio-transcriptions` | `POST /v1/audio/transcriptions`; `usage` may be `null` | `audio.write` | §8.6 |
| `files-api` | `/v1/files` create, retrieve, content, list, delete | `files.read`, `files.write` | Files API design |
| `uploads-chunked` | `/v1/uploads` create, parts, complete, and a resume read from a fresh client | `files.read`, `files.write` | Files API design |
| `file-input` | `input_file { file_id }` on `/v1/responses` | `files.write`, `responses.write` | Files API design |
| `no-usage-limits` | no `RateLimit` or `RateLimit-Policy` headers | `models.read` | §12.1 |

## Commands

The commands below are run from this directory.

### 1. Install

```sh
cd conformance/node
npm ci
```

### 2. Provision a test project and two keys

The live suite needs two keys:

* a **default** key holding every scope the suite uses (`models.read`,
  `responses.read`, `responses.write` and each feature's scopes above);
* a key that holds **only `models.read`**, used by the 403 `insufficient_scope`
  test.

The script below creates a `test`-environment project and both keys through the
developer console API, signed in as a console admin.

* It **asks for the scopes explicitly**. Keys keep the scopes they were created
  with (CONTRACT-3 §7), so a key created with "the default" on a stack that
  predates a scope never gains it.
* A scope the stack's console refuses as unknown (the stack predates that
  feature) is dropped and recorded. Any other refusal, or a key created without
  a scope that was asked for, stops the script.
* It prints only ids, scopes and the last four characters of each key.
* It writes the secrets to `--out` with mode 0600, together with
  `TECHSARA_API_KEY_SCOPES` and `TECHSARA_STACK_UNKNOWN_SCOPES`.
* It refuses any `--out` inside a git work tree (it walks up looking for
  `.git`). This repository is public, and the root `.gitignore` does not cover
  `*.env`.

```sh
node scripts/provision-key.mjs \
  --console "$CONSOLE_ORIGIN" \
  --email "$ADMIN_EMAIL" \
  --password-file "$ADMIN_PASSWORD_FILE" \
  --out "$HOME/.config/techsara/conformance.env"
```

`$CONSOLE_ORIGIN` is the orchestrator origin that serves `/auth/login` and
`/admin/api/developers/*`, for example `http://localhost:8080`.

**Re-provision when a stack gains a feature.** Before each planned test the
suite compares the feature's scopes with `TECHSARA_API_KEY_SCOPES`:

* a scope the key holds, or one the stack did not know at provisioning: the
  test runs;
* a scope the stack knew but the key lacks: SKIP, "re-provision";
* scopes not recorded (a hand-made key): the test runs, and a
  `403 insufficient_scope` becomes the same SKIP.

### 3. Run

```sh
TECHSARA_BASE_URL="https://api.example.test/v1" \
TECHSARA_ENV_FILE="$HOME/.config/techsara/conformance.env" \
npm test
```

| script | runs |
|---|---|
| `npm test` | offline and live suites; table in `results/table.md` |
| `npm run test:offline` | SDK-behaviour tests against local stubs only (no stack needed); `results/table-offline.md` |
| `npm run test:live` | the live suite only; `results/table-live.md` |
| `npm run selftest` | the suite's own test bodies against `selftest/design-stub.mjs` (no stack): every planned test promoted (all must pass) and planned (all must XPASS-fail); the three streaming tests against three stream shapes; the scope SKIP rule |

| variable | default | meaning |
|---|---|---|
| `TECHSARA_BASE_URL` | none | Base URL **including `/v1`**. If unset, every live test is SKIPPED. There is deliberately no default. |
| `TECHSARA_API_KEY` | none | The default key. |
| `TECHSARA_API_KEY_SCOPES` | unset | The default key's scopes, space-separated (written by step 2). |
| `TECHSARA_STACK_UNKNOWN_SCOPES` | unset | Scopes the stack refused as unknown at provisioning (written by step 2). |
| `TECHSARA_API_KEY_NARROW` | none | A key holding only `models.read`. If unset, the two scope tests are skipped. |
| `TECHSARA_ENV_FILE` | none | A `KEY=value` file (the output of step 2). Variables already set in the environment win. |
| `TECHSARA_CHAT_MODEL` | `techsara-35b` | The model used by the generating tests. |
| `CONFORMANCE_MIN_INTERVAL_MS` | `1100` | The minimum gap between two API requests across the whole run (see "Load" below). Set it to `0` on a stack without limits. |
| `CONFORMANCE_BUILT_FEATURES` | empty | Comma-separated feature ids to treat as built. |
| `CONFORMANCE_OUTPUT_CEILING` | `1000000` | The deployment's `PUBLIC_API_MAX_OUTPUT_TOKENS`. `techsara-35b` must advertise min(this, its context window). |
| `CONFORMANCE_ALLOW_LONG_GATE` | unset (skipped) | Opt-in, `1`. Runs the planned 1,000,000-token request, which holds the fleet-wide `main.long` capacity slot (see "Load"). A 503 `model_unavailable` while another long request holds the slot is a SKIP. |
| `CONFORMANCE_LONG_REQUEST_SECONDS` | `0` (skipped) | Opt-in. Holds one synchronous request open at least this long, for example `330`, past Node fetch's 300 s headers timeout. It makes the engine generate for that long. It is meaningful only against an origin reached directly: through a Cloudflare-proxied hostname a non-streaming response is cut at about 100 s (HTTP 524). On a stack whose `max_output_tokens` ceiling is 8,192 it fails at validation. |

### Which hop a run covers

`TECHSARA_BASE_URL` decides what is under test, and the table records it.

* **The orchestrator origin** (`http://localhost:<orchestrator-port>/v1`) tests
  the API implementation alone.
* **The Next `/v1` edge** (`http://localhost:<frontend-port>/v1`, the path the
  public hostname takes) also tests what the edge must preserve: header
  forwarding, unbuffered streams, and the edge's own refusals.
* **The public hostname** adds Cloudflare and its 100 s origin response
  timeout.

Run a release candidate through the edge at least once. The two can differ, as
the 2026-09-13 runs below show.

## What the live suite checks

* **Models** (`test/live/models.test.mjs`)
  * `models.list` and `models.retrieve`;
  * 404 `model_not_found`, not retried;
  * `X-Request-Id` present;
  * no internal checkpoint, engine or URL names anywhere in the list;
  * `openapi.json` declares `Retry-After` on the 503 of both generating
    operations.
* **Responses** (`test/live/responses.test.mjs`)
  * **Sync**:
    * shape, `output_text` and consistent `usage`;
    * message-list input with instructions and metadata;
    * `top_p` refused with a 400 that names it;
    * `stream` together with `background` refused;
    * `max_output_tokens` above the advertised ceiling refused;
    * 404 `response_not_found`;
    * a user message with an `http://` image part refused with 400. On a stack
      without image input this 400 is "content must be a string", so the SSRF
      rule is asserted separately by the planned `image-input` test.
  * **Stream**, checked against CONTRACT-3 §10 invariants only:
    * `created` first, then an optional `queued` (sent while the main engine is
      recovering), then `in_progress`;
    * at least one delta, `output_text.done` after the last delta;
    * exactly one terminal event, and it is last;
    * `sequence_number` runs 1, 2, 3 …;
    * deltas, `done.text` and the terminal snapshot agree;
    * `usage` is null before the terminal event and measured on it;
    * `output_item.*` and `content_part.*` frames are allowed anywhere
      non-terminal;
    * SSE headers.
  * **Stream frames the SDK helper needs** (a separate test):
    `output_item.added` and `content_part.added` before the first delta,
    `content_part.done` and `output_item.done` after `output_text.done`, and one
    item id used by every `item_id`, the item and the terminal snapshot's
    message. The `responses.stream()` helper test shows what happens without
    them. `npm run selftest` proves that one server can pass the order test,
    the frame test and the helper test at the same time.
  * **Background**:
    * create, then poll to `completed`;
    * cancel, and cancel again idempotently. If the generation finishes before
      the cancel lands, the test is a SKIP, because cancellation was not
      exercised.
* **Chat Completions** (`test/live/chat.test.mjs`)
  * sync with `max_tokens`;
  * `finish_reason: length`;
  * a stream with `include_usage` (one final usage chunk, `choices: []`) and one
    without;
  * `n` refused with a 400 that names it.
* **Errors** (`test/live/errors.test.mjs`)
  * 401 for a malformed key, a bad checksum and a missing header, not retried;
  * 403 `insufficient_scope` with the narrow key, not retried, while the same
    key can still list models;
  * 404 `model_not_found`;
  * 404 envelope for an unknown path;
  * 413 `request_too_large`;
  * `Idempotency-Key` replay returns the same id, status and usage. Equal usage
    does not prove the engine was not run again (a second "ok" measures the
    same), and a `/v1` caller cannot observe that;
  * the replayed output text;
  * 409 `idempotency_conflict`;
  * every error checked against the §9 envelope: `code`, `type`, `param`, and
    a `request_id` equal to `X-Request-Id`.
* **No client timeout** (`test/live/timeout.test.mjs`)
  * the no-timeout client completes a sync and a streamed request;
  * the opt-in long request.
* **Planned endpoints** (`test/live/planned-endpoints.test.mjs`): embeddings,
  rerank, audio, files, chunked uploads with resume, and file input.

**A 503 "at capacity" is not provoked live.** It needs a saturated engine, and
this suite must not create one on a shared stack. The SDK side of `Retry-After`
is proven offline, and the schema side by the OpenAPI test.

## openai-node facts this suite measured (offline, `test/offline/`)

These tests run against local stubs, so they need no stack. They pin what a
TechSara caller using openai-node 7.15.0 gets.

1. **`timeout: Infinity` does not disable the timeout. It aborts at once.**
   * Node coerces a timer above 2^31−1 ms to 1 ms (`TimeoutOverflowWarning`),
     and the SDK aborts through `setTimeout`.
   * A per-request `timeout: Infinity` is refused with "timeout must be an
     integer".
   * The largest working value is `2**31 - 1` ms (24.8 days).
2. **The SDK timer is not the only client timer.** Node's built-in fetch
   (undici) has a 300 s `headersTimeout` of its own.
   * Measured on 2026-09-13 with Node v22.23.2 (bundled undici 6.28.0): a
     non-answering server gave `APIConnectionTimeoutError` after **300.7 s**,
     cause `UND_ERR_HEADERS_TIMEOUT`, even though `timeout` was `2**31-1`.
   * Removing every client-side timer needs both settings:

   ```js
   import OpenAI from 'openai';
   import { Agent, fetch } from 'undici';
   const client = new OpenAI({
     baseURL, apiKey,
     timeout: 2 ** 31 - 1,
     fetch,                                   // undici's fetch, matching the Agent
     fetchOptions: { dispatcher: new Agent({ headersTimeout: 0, bodyTimeout: 0 }) },
   });
   ```

   * **What this is for:** long **streams**, and synchronous calls to an origin
     reached directly. It does **not** make a long synchronous call work
     through the public hostname. There, Cloudflare answers 524 after 100 s
     with no byte, and the Next `/v1` edge has its own 300 s timeouts
     (CONTRACT-3 §8.3). Above about 5,000 output tokens, use `stream: true` or
     `background: true`, always with an `Idempotency-Key`.
   * A stream is also protected by the server heartbeat. undici's `bodyTimeout`
     measures silence between chunks, and a `: ping` comment resets it. The SDK
     never surfaces the comment. Without heartbeats the same quiet stream is
     cut.
3. **`Retry-After` is obeyed only up to 60 s.** A larger value falls back to the
   SDK's own 0.5–8 s backoff.
   * By default the SDK retries 408, 409, 429 and ≥ 500 **twice**.
   * TechSara's values (1, 2, 5, 20, 30 s) are all obeyed.
   * Two default retries do not outlast a planned engine restart (measured
     recoveries of 172–188 s). A larger `maxRetries` rides one out, but only
     together with an `Idempotency-Key` (fact 4).
4. **A 504 `timeout` on `POST /v1/responses` is retried by default.**
   CONTRACT-3 §8.3 answers a synchronous generation cut by its wall clock with
   504. Without an `Idempotency-Key` every retry starts a **new** generation.
   A key passed in `headers` goes out unchanged on every attempt, so the retry
   becomes a replay.
5. **Every 409 is retried by default**, including a permanent
   `idempotency_conflict` (different body): that costs three requests. The SDK
   honours `x-should-retry: false`.
6. **openai-node sends no `Idempotency-Key`**, and **its `idempotencyKey`
   request option is silently dropped**, because the client never sets
   `idempotencyHeader`.
   * Pass `{ headers: { 'Idempotency-Key': key } }`.
   * The sidecar rule "an Idempotency-Key header is 400" therefore does not break
     SDK calls.
7. **`embeddings.create` without `encoding_format` asks for `base64`**, and the
   SDK decodes it. The server must implement base64.
8. **Terminal stream events**:
   * An `error` event, or a Chat Completions error chunk, is thrown as an
     `APIError` with `status: undefined` and the envelope's `code`.
   * A `response.failed` event is delivered as an ordinary event, not thrown, so
     callers must check the terminal `type`.
9. **`client.responses.stream()` fails on the CONTRACT-3 §10 event set** with
   "missing output at index 0". The helper's accumulator needs
   `response.output_item.added` and `response.content_part.added` before the
   first delta, and `content_part.done` and `output_item.done` after it, with
   the item's `id` equal to every event's `item_id`. With those four frames
   added, the same stream resolves `finalResponse()` correctly. The offline
   test proves both directions.

## Load

The generating tests ask for at most 16 output tokens, except:

* the cancel test, which requests 600 tokens and cancels at once;
* the planned 1,000,000-token test, which is **opt-in**
  (`CONFORMANCE_ALLOW_LONG_GATE=1`). The answer is one word, but the request's
  planned output is above 131,072 tokens, so it takes the `main.long` capacity
  gate (CONTRACT-3 §11, §12.3). That gate admits **one** public request
  fleet-wide. While the test holds it, a customer's long job waits and may get
  503; while a customer holds it, the test gets 503, which it reports as SKIP.
  Enable it only on a stack nobody else is using.

Requests run serially and are paced across test-file processes
(`results/.last-request-ms`). Stacks built before the "no usage limits" decision
still enforce 60 requests per minute per project. A full run makes roughly
90–95 API calls and takes about 100 seconds of live testing.

## Last recorded runs

These runs were made on 2026-09-13 against the isolated e2e stack (an
orchestrator image built before the six-models, Files and no-limits work). They
used a fresh `test` project provisioned by step 2, whose console refused
`embeddings.write`, `rerank.write`, `audio.write`, `files.read` and
`files.write` as unknown. The same commands and key were used for both hops.

| base URL | PASS | FAIL | XFAIL | SKIP | total |
|---|---:|---:|---:|---:|---:|
| orchestrator origin | 59 | 3 | 27 | 3 | 92 |
| Next `/v1` edge | 58 | 4 | 27 | 3 | 92 |

**FAIL on both hops** (real findings, not test errors):

* `the text is framed by output_item.added …` and `the responses.stream()
  helper resolves finalResponse()`: one root cause. The stack sends exactly the
  §10 list (`created → in_progress → delta → output_text.done → completed`), so
  the helper throws "missing output at index 0" (fact 9 above).
* `a replayed response carries the original output text`: a same-key,
  same-body replay returns the original id, status and usage with
  `output: []`, so `output_text` is `''`.
  * CONTRACT-3 §13 says "the original response is returned".
  * §16 says output is not stored.
  * One of the two has to change.

**FAIL through the edge only**:

* `a body over the 1 MiB text limit is 413 request_too_large`: the edge refuses
  the body itself with an envelope whose `request_id` is `null` and no
  `X-Request-Id` header. The edge source says this is deliberate (the request
  never reached the orchestrator). CONTRACT-3 §7 says every response carries
  `X-Request-Id`. One of the two has to change. The same 413 from the
  orchestrator passes.

**XFAIL** (expected, all for the planned-feature reason):

* `six-models` ×5;
* `one-million-output` ×5;
* `image-input` ×3;
* `embeddings` ×3;
* `max-completion-tokens` ×2;
* `audio-transcriptions` ×2;
* `files-api` ×2;
* `uploads-chunked` ×2;
* `rerank` ×1;
* `file-input` ×1;
* `no-usage-limits` ×1.

**SKIP**: the live at-capacity 503 (see above), the opt-in long request, and the
opt-in 1,000,000-token request.

`npm run selftest` on the same day:

* all 28 planned tests pass when promoted, and all 28 XPASS-fail when planned;
* stream shape `contract`: order PASS, frames FAIL, helper FAIL;
* stream shapes `helper` and `queued`: all three PASS;
* the scope rule: a stale key SKIPs, and a scope the stack did not know runs.
