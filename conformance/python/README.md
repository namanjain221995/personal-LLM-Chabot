# TechSara `/v1` conformance suite — Python (official `openai` SDK)

A pytest suite a release manager points at **any** TechSara `/v1` base URL with
a test key. It drives the official `openai` Python SDK, pinned in
`requirements.txt`. Where the SDK hides the wire (SSE comments, headers, the
rerank route, a request body cut off mid-transfer) it uses raw `httpx`. Every
promise in `docs/developer-platform/CONTRACT.md` it can reach ends up in one of
five results:

| result | meaning |
|---|---|
| `PASS` | the target does what the contract says |
| `FAIL` | the target claims the feature (or it is core) and gets it wrong |
| `XFAIL` | **not built yet**: the feature is planned and the target does not have it (strict, see below) |
| `XPASS(strict)` | reported as a failure: the test passed although the target's own schema does not declare the feature. Either the schema is behind the code, or the test proved nothing |
| `SKIP` | could not run, with the reason (no limited key; capacity probe not enabled) |

At the end of the run you get a table with one line per test, its feature, the
reason for every `FAIL` and the observed failure behind every `XFAIL`.

## What it covers

| area | module | feature marker |
|---|---|---|
| models list / retrieve / 404 | `test_models.py` | core; `model_catalogue`, `output_ceiling`, `limits_off` |
| responses, sync: text, usage, read back, refusals; the 1,000,000 ceiling accepted, and one above it refused only when the model advertises exactly that ceiling | `test_responses.py` | core; `output_ceiling` |
| responses, stream: event order, `sequence_number`, usage only on the terminal event, deltas = done text, raw SSE headers | `test_responses_stream.py` | core; `output_ceiling` |
| responses, background: 202 → poll → completed; cancel → cancelled, idempotent; a cancel never walks back a finished response | `test_background.py` | core |
| chat.completions sync + stream with `max_tokens`; `max_completion_tokens` | `test_chat_completions.py` | core; `max_completion_tokens`, `output_ceiling` |
| error envelope: 401 bad key (four credential shapes), 403 scope, 404 model, internal names never resolve, 409 idempotency conflict, replay, per-endpoint key scope | `test_errors.py` | core |
| retry semantics as the SDK really behaves (event hooks on its own transport): 401 not retried; a permanent 409 on every retry; 409 + `Retry-After` while the first request runs, and the SDK then collects the original; 503 at capacity + `Retry-After` | `test_retry_semantics.py` | core; `idempotency_in_flight_409`; `capacity_gates` + `capacity_probe` |
| `timeout=None` long request: stream and sync run to their output budget; the stream never has more than 15 s + 5 s slack with no byte on the wire after its headers (timed inside the SDK's transport, so `: ping` comments count) | `test_long_request.py` | core, marker `long` |
| embeddings (SDK default base64 path, float, refusals) | `test_embeddings.py` | `embeddings` |
| rerank (raw httpx), id `rrk_<24 hex>` | `test_rerank.py` | `rerank` |
| audio.transcriptions (json / text / verbose_json, refusals) | `test_audio_transcriptions.py` | `audio_transcriptions` |
| image input on both routes, vision + OCR models, `data:`-only rule | `test_image_input.py` | `image_input` |
| files, chunked uploads, resume after a lost part list, idempotent part retry, a part cut mid-transfer, a file as model input | `test_files_uploads.py` | `files`, `uploads`, `uploads_resume`, `files_model_input` |

## Built or planned: how the suite decides

A test for a planned feature carries `@pytest.mark.feature("<name>")`. At
start-up the suite reads the target's own public schema
(`GET /v1/openapi.json`, no key needed). The schema says what the server claims
to have built (`techsara_conformance/features.py`):

| feature | counts as built when `/v1/openapi.json`… |
|---|---|
| `model_catalogue` | schema `Model` declares `kind` |
| `output_ceiling` | schema `Response` declares `incomplete_details` |
| `max_completion_tokens` | schema `ChatCompletionRequest` declares `max_completion_tokens` |
| `idempotency_in_flight_409` | the `POST /v1/responses` 409 description says "still running" |
| `limits_off` | no operation documents a 429 |
| `capacity_gates` | the `POST /v1/responses` 503 description says "at capacity" |
| `image_input` | mentions the `input_image` part |
| `embeddings` / `rerank` / `audio_transcriptions` | has `POST /v1/embeddings` / `/v1/rerank` / `/v1/audio/transcriptions` |
| `files` / `uploads` | has `POST /v1/files` / `POST /v1/uploads` |
| `uploads_resume` | has `GET /v1/uploads/{id}` |
| `files_model_input` | mentions the `input_file` part |

A planned feature's tests still **run**, marked `xfail(strict=True)`. A pass is
therefore a failure (`XPASS(strict)`), never a quiet green. The header and
the summary both print every feature's state and the evidence for it.

To override detection, for example so that a release candidate that claims
embeddings gets `FAIL` rather than `XFAIL` when they are missing:

```bash
--feature embeddings=built --feature rerank=built     # repeatable; also =planned or =auto
TECHSARA_FEATURES="embeddings=built,rerank=built"      # same, from the environment
```

Refusal tests guard against passing for the wrong reason. A server that
refuses **every** image part would otherwise "pass" the `http://` image
refusal. So the image refusal tests first check that a valid `data:` image is
accepted. The `max_completion_tokens` both-fields refusal first checks that
the field is accepted on its own. Both traps were hit on the first e2e run.

## Install

```bash
cd conformance/python
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`openai` 3.x sends every request through `httpx2`, and that is pinned too.
`httpx` is only the raw client. The run header and the JSON report record all
three versions.

Python 3.11+ (run on 3.12.3).

## Keys

The suite needs one key with the server's **default** scopes, minted on the
server version under test. It can use a
second key holding only `models.read`: without it the two 403 tests `SKIP`.
`tools/provision_key.py` mints both through the console API with a console
account that holds `api.keys.create`. It writes them to a 0600 JSON file and
prints only the key prefixes:

```bash
.venv/bin/python tools/provision_key.py \
  --origin http://127.0.0.1:8081 \
  --email <console-account-email> \
  --password-file <file-with-the-password> \
  --out keys.json
```

`--origin` is the orchestrator or the web front door; the console API is not
under `/v1`. A stack that still enforces usage limits spends the default budget
within seconds. On such a stack, add `--lift-limits` to raise the new project's
ceilings. This needs `api.limits.manage` (super admin). It was **not** exercised
on 2026-09-13: the e2e admin account lacks that capability and got
`404 Not found.`. See "Enforced limits" below for what the suite does instead.

**Run `provision_key.py` again after every rebuild that adds scopes.** A key
keeps the scopes it was minted with (CONTRACT-3 §7): a key made before
2026-09-13 holds no `embeddings.write`, `rerank.write` or `audio.write`, even
on a server that now grants them by default. The suite reads `"scopes"` from
the keys file. When a feature is **built** but the key lacks its scope, that
feature's tests are `SKIP`, and the reason names the missing scope. They are
not reported as `FAIL`. `--strict-scopes` turns this into a usage error. Tests
that use only the limited key are never skipped for this. Files features
require no scope yet, because CONTRACT.md does not name the Files scopes.

To bring your own keys, set `TECHSARA_API_KEY` and optionally
`TECHSARA_LIMITED_API_KEY`. Set `TECHSARA_API_KEY_SCOPES` (comma-separated) if
you want the scope check. Without it the key's scopes are unknown and nothing
is skipped for scope. Keys are never accepted on the command line.

## Run

```bash
# against the isolated e2e stack, with the provisioned keys
.venv/bin/python -m pytest --keys-file=keys.json --conformance-report=conformance-report.json

# against any deployment
TECHSARA_BASE_URL=https://<your-techsara-host>/v1 \
TECHSARA_API_KEY=tsk_test_… TECHSARA_LIMITED_API_KEY=tsk_test_… \
  .venv/bin/python -m pytest

# one area
.venv/bin/python -m pytest --keys-file=keys.json tests/test_responses_stream.py

# a release run on a QUIET stack: a long request that outlasts the SDK's 600 s default
.venv/bin/python -m pytest --keys-file=keys.json -m long --long-output-tokens 60000

# the capacity probe: ONLY on a stack whose engines nobody else is using
.venv/bin/python -m pytest --keys-file=keys.json -m capacity_probe --capacity-probe
```

Write file-path options with `=` (`--keys-file=keys.json`). pytest reads a
separate `--keys-file keys.json` as a test path and then cannot find
`pytest.ini`.

| option / variable | default | purpose |
|---|---|---|
| `--base-url` / `TECHSARA_BASE_URL` | none (required, or from the keys file) | origin or `/v1` URL |
| `--keys-file` / `TECHSARA_KEYS_FILE` | none | JSON written by `provision_key.py` |
| `TECHSARA_API_KEY`, `TECHSARA_LIMITED_API_KEY` | from the keys file | keys |
| `--feature NAME=built\|planned\|auto` / `TECHSARA_FEATURES` | auto | override detection |
| `--long-output-tokens` / `TECHSARA_LONG_OUTPUT_TOKENS` | 1024 | size of the two `long` tests |
| `--capacity-probe` | off | run the gate-filling test |
| `--no-limit-wait` | off | do not wait out a usage-limit 429 |
| `--strict-scopes` | off | usage error instead of `SKIP` when a built feature needs a scope the key lacks |
| `TECHSARA_API_KEY_SCOPES` | unknown | scopes of `TECHSARA_API_KEY`, for the scope check |
| `--conformance-report=PATH` | none | write the result table as JSON |
| `TECHSARA_{CHAT,VISION,OCR,EMBED,RERANK,WHISPER}_MODEL` | the six contract ids | a deployment that names a model differently |
| `TECHSARA_SMALL_OUTPUT_TOKENS` | 16 | the budget of every ordinary generation |
| `TECHSARA_EXPECT_CHAT_MAX_OUTPUT_TOKENS` | 1000000 | the advertised chat ceiling |
| `TECHSARA_BACKGROUND_POLL_TIMEOUT_S`, `TECHSARA_REQUEST_TIMEOUT_S` | 180, 180 | client-side bounds |

## Cost and safety

The suite may be pointed at engines people are using, so it is sized for that.
**Check where the target's engines are before a run.** A staging or e2e stack
can share its engines with production, and then every generating test below
puts load on the production engine.

* Every ordinary generation asks for 16 output tokens or fewer (the
  background-cancel test asks for 256 and is cancelled at once; the in-flight
  idempotency test asks for 200). Tests run one at a time; the only concurrent
  pair is the in-flight idempotency test.
* The two `long` tests default to 1,024 output tokens each, about ten seconds
  of decode. Raise `--long-output-tokens` only on a quiet stack.
* The capacity probe holds the `main.long` gate with a 200,000-token stream
  for about 35 s. It is `SKIP` unless `--capacity-probe` is given. No other
  test tries to provoke a 503.
* `test_max_output_tokens_of_one_million_is_accepted_and_clamped_not_refused`
  (feature `output_ceiling`, marker `main_long_gate`) plans 1,000,000 output
  tokens. That puts it over the 131,072-token long-footprint threshold, so
  while it runs it holds `main.long`. That is the **one** public long slot,
  shared by every project (§12.3). The model stops after one word, so it is
  held only briefly. A customer's long request that arrives in that moment
  waits. If a customer's long job already holds the slot, this test waits up
  to 30 s, then gets `503 model_unavailable` "at capacity" and reports `SKIP`
  with the reason. Where even a short wait for a customer is unacceptable,
  deselect it with `-m "not main_long_gate"`.
* **Limits off means no patience.** When the target's own schema says limits
  are off (`limits_off` built, or `--feature limits_off=built`), the suite
  neither paces nor waits out a 429. Any usage-limit 429 reaches the test that
  hit it. After the run, if any `RateLimit` / `RateLimit-Policy` header or
  usage-limit 429 was seen at all, the suite adds a `FAIL` row
  (`session::limits_off_held_for_the_whole_run`) and exits non-zero, even when
  every test passed.
* **Enforced limits.** Deployments are meant to run with no usage limits
  (`PUBLIC_API_ENFORCE_LIMITS=false`). A target whose schema still documents
  429s sends `RateLimit: "requests";r=…;t=…`, and the suite waits before the
  request that would be refused. A token-budget 429 does not show in that
  header: one request asking for 1,000,001 output tokens reserves 60,000, and
  needs an empty minute. The client transport waits out a 429 whose code is
  `rate_limit_error`, `quota_exceeded` or `concurrency_limit_exceeded`,
  honouring `Retry-After`. It never does this for a request carrying an
  `Idempotency-Key`, because there the 429 may be the behaviour under test.
  The summary prints every wait, and the `limits_off` row reports that limits
  are on.

## Self-test (no server)

```bash
.venv/bin/python -m pytest selftest -p no:techsara_conformance.plugin
```

This pins feature detection, the SSE reader and wire clock, the pacing
parser, the limit-patient transport, the limits-off policy, the scope check
and the generated PNG/WAV fixtures. `selftest/test_suite_against_a_mock_target.py`
also runs the real suite as a subprocess against `selftest/mock_target.py`, a
local contract-shaped mock. That covers each defect the 2026-09-13 review
proved: a hidden 429 on a limits-off target, a paced heartbeating stream, a
contract-shaped rerank id, a key minted without a new scope, a ceiling refusal
from a server that does not advertise the ceiling, and `main.long` at
capacity. It needs no server, no key and no engine, and takes about 15 s.

## Not covered, said plainly

* The error envelope **inside** a stream (`response.failed` / an error chunk).
  Provoking one needs an engine failure or a wall-clock stop.
* The 15 s heartbeat is observable only when the stream would otherwise be
  silent (a long prefill or a queue wait). The long stream test asserts that
  no byte-free gap on the wire after the headers exceeds 20 s, and it reports
  how many `: ping` lines it saw. It does not provoke a silent phase, so on a
  fast engine it proves the stream is never silent, not that the heartbeat
  exists. The wait before the headers (a capacity gate may hold a request up
  to 30 s there, §10) is reported but not asserted.
* The in-flight idempotency test gives the first request a fixed 0.4 s head
  start. Against a remote target with high latency, the second request can
  claim the key first and see `200`, which is a false `FAIL`.
* `504 timeout` at the per-request wall clock: the shortest one is 4,200 s.
* Transcription uses a generated tone, not speech, so the assertions are about
  the response shape only.
* The Files / Uploads shapes follow the Files API design as it stood on
  2026-09-13. That design was still in progress, and `uploads_resume`
  (`GET /v1/uploads/{id}`, `part_number`) is a TechSara extension proposed
  there. If the final contract differs, these tests change with it.
* Only the Python SDK. A Node conformance run is a separate deliverable.
