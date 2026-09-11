# Evidence — upload reliability (2026-09-10)

Every number here comes from a run named next to it. Commands and counts are copied, not summarised. Sections are filled as the work lands; an empty section is unverified.

## Deployed revision at the start

`1ded7fd` on both containers (orchestrator image `sf-local-ai-orchestrator:cuda`, frontend `sf-local-ai-frontend:portable`, started 2026-09-10T14:03:50Z after a host reboot at 19:33 IST on both nodes).

## Before: fixtures through the Next.js proxies on `1ded7fd`

Synthetic fixtures (test pattern + tone, no personal content), `scripts/video_smoke.py --via-frontend http://127.0.0.1:3000`, account video-smoke-2, quiet box:

| fixture | upload | first answer | follow-up | stages (s) |
|---|---:|---:|---:|---|
| fixture_20mb | 0.1 s | 78.8 s | 5.4 s | (status route not proxied) |
| fixture_200mb | 0.6 s | 148.7 s | 4.4 s | (status route not proxied) |

The 200 MB file went as three 64 MiB parts. Both answers cited frames and timestamps from the fixture (evidence=8 chunks each).

## Review

Nine agents read 375 file entries (168 every line, 193 in named ranges, 14 excluded with reasons) and returned 131 findings: 1 critical, 28 high, 52 medium, 37 low, 13 info; 90 confirmed by code, 27 plausible risks, 14 observations. The completeness critic confirmed all 9 findings it tried to refute and named 14 uncovered files, each assigned to an implementer or recorded in REVIEW-MANIFEST.md.

## Tests run so far (contract layer)

| command | result |
|---|---|
| `orchestrator/.venv/bin/python -m pytest tests/test_upload_reliability_schema.py tests/test_video_understanding.py tests/test_history.py tests/test_document_uploads.py` (db techsara_video_test) | 82 passed, 1 skipped |
| `… tests/test_upload_reliability_schema.py tests/test_history.py tests/test_history_v3.py` (after the conditional replace) | 32 passed |
| `python3 .github/workflows/scripts/schema_parity.py invariants` | migration table OK: V1..V29 |
| `cd frontend && npx tsc --noEmit -p .` (after the type contract) | exit 0 |

## Migration safety (V29), 2026-09-11

CI's own parity tool, run locally against throwaway databases:

| check | command | result |
|---|---|---|
| fresh install | `schema_parity.py upgrade` on an empty database | `upgraded 0 -> 29` |
| upgrade from V20 | `stage --to 20` then `upgrade` | `upgraded 20 -> 29` |
| upgrade from V28 (production's current version) | `stage --to 28` then `upgrade` | `upgraded 28 -> 29` |
| fresh vs upgraded-from-20 | `compare` | schemas are IDENTICAL (971 structural lines) |
| fresh vs upgraded-from-28 | `compare` | schemas are IDENTICAL (971 structural lines) |
| idempotence | `upgrade` again on the V29 database | `upgraded 29 -> 29`, nothing written |
| migration table | `schema_parity.py invariants` | V1..V29, contiguous, unique, non-empty |

V29 is additive only: two new tables and two nullable columns on
`video_analyses`. Rollback is therefore "run the previous image": the older
code ignores both tables and both columns, and no existing column changed
type or nullability. Dropping the tables is possible but unnecessary and is
not part of the rollback procedure.

## Implementation runs (branch `feat/upload-reliability`)

Four streams, disjoint file ownership, each tested on its own database.
Integration and every figure below is the Principal Engineer's own run.

| suite | command | result |
|---|---|---|
| backend, whole | `orchestrator/.venv/bin/python -m pytest tests -q` (db `techsara_video_test`) | **3115 passed, 5 skipped** |
| frontend, whole | `npx vitest run` | **1920 passed**, three consecutive runs |
| types | `npx tsc --noEmit -p .` | exit 0 |
| lint | `npm run lint` | 0 errors, 1 pre-existing `<img>` warning |
| production build | `npm run build` | Compiled successfully |
| correctness gate | `ruff_gate.py orchestrator sync-worker launcher scripts .github/workflows/scripts` | clean |
| aarch64 gate | `arm64_gate.py` over all five Dockerfiles | OK |
| compose | `docker compose … config --services` with the full `-f` chain | parses; `frontend.stop_grace_period = 5m0s` |

### Three defects the full suites found that the isolated runs had not

Each passed when its own file was run alone and failed when the suite ran
together — which is the only way they could have reached production.

1. A new `ConversationChanged` exception shadowed the one the truncate guard
   already raised, so its handler read the wrong attributes and turned a 409
   into a 500. Renamed to `ThreadMoved`.
2. The metrics allow-list edits were among the files lost in the interrupted
   session, so every new label value folded to `other` and five intent tests
   read absent counters. Restored, and the intent tests now reset the
   process-global counters per test.
3. A test drain helper bounded by a fixed number of fake-timer advances hung
   under load, because the 64 MiB SHA-256 it was waiting on resolved later
   than the advances. Re-bounded on real time.

## QA matrix — end to end on an isolated stack

`scripts/e2e-stack.sh` runs the branch's own images against its own database
(`techsara_e2e_test`), its own `/data` and `/reports` volumes and loopback
ports 8081/3001. It shares only the model engines, which hold no per-user
state. Production was not touched: it still runs `1ded7fd` at schema 28.

`scripts/qa_upload_matrix.py`, twice — once at the orchestrator, once through
the Next.js route handlers, which is the path a browser takes:

| # | scenario | orchestrator | via proxies |
|---|---|---|---|
| 1 | a normal MP4, selection → persisted answer | pass (50.4 s, 2,449 chars) | pass |
| 2a | just below / just above the 90 MiB chunk threshold | pass (89.6 MB single-shot, 98.7 MB chunked) | pass |
| 2b | an oversize declaration refused before any byte | pass (413) | pass |
| 4b/4c/4d/5a | a part cut mid-body is not accepted; resume sends only the rest; complete replays | pass (2 parts, 1 resent, identical replay) | pass |
| 12f | a traversing filename reduced to a basename | pass (`passwd.mp4`) | pass |
| 13a | another account on this session: GET/PUT/COMPLETE/DELETE | pass (404 on all four) | pass |
| 5b/10a | the same intent twice | pass (one generation, one stored answer) | pass |
| 14b | a client that sends no `intent_id` | pass | pass |
| 7/8 | every viewer leaves mid-analysis | pass (cut at the first step; 2,535 chars stored) | pass |
| 6 | reload mid-analysis re-attaches | pass (45.7 s, no restart) | pass |
| 12e | a corrupt file | pass (a sentence naming the failure) | pass |
| 3d | a 20 MB and a 200 MB video in one turn | pass (174.2 s, 4,457 chars) | not run |

**11/11 at the orchestrator, 11/11 through the proxies, 12/12 with the heavy
pairing.** The cut in 4b is a real one: a raw socket declares a
Content-Length and then closes, because an HTTP client refuses to send that.

### Scenario 11 — the restart, which is the failure that started this

`scripts/recovery-tests/restart_drill.py`, against the isolated stack:

```
before the restart: analysis at 'frames running', attempt 1, 2 stage(s) already durable
restarting the orchestrator…
back after 6.3s (healthy 5.2s after start)
the server says the send is: interrupted (resumable=True, live=False)
RESULT
  request      interrupted -> resumed -> completed (attempt 2)
  answer       1 row, 2512 chars, stored server-side
  analysis     done, attempt 1 -> 2 (stages already durable before the restart were not re-run)
```

Under the old code this is exactly the shape that produced the blank
assistant message of 2026-09-09: the generation died with no record. An
intermediate run of the same drill, inspected in the database before the
resume, showed the state the fix produces — request `interrupted`,
`resumable`, analysis `done` with 9 durable stages, and **zero** assistant
rows where the old code left an empty one.

## Measured: stages and sizes

From `video_analyses` on the isolated stack (its own cold caches; the engines
are shared with production, which was serving normally throughout):

| file | analysis wall | transcript | OCR | captions | fusion | frames read |
|---|---:|---:|---:|---:|---:|---|
| 200 MB | 106 s | 9 s | 64 s | 30 s | 6 s | 26/30 |
| 20 MB (five runs) | 53–68 s | 1 s | 31–37 s | 11–20 s | 7–11 s | 11–13 of 15 |

Upload sessions recorded: 8 complete (111 MB average), 8 cancelled, 1 left
open by the interrupted-part scenario — which is the point of the row.

**These are not a like-for-like latency comparison with the "before"
numbers above.** Different database, cold caches, different content, and the
shared engines were under production load in both cases. What they show is
that the reliability work did not cost a step change in time: a 20 MB video
still answers in about a minute and a 200 MB one in under three.

**"Frames read" is the honesty fix, visible.** `26/30` and `13/15` are not
failures: the OCR engine returned looping output for those frames and the
new detector caught it, so the stage reports a partial read and the
understanding says a stage was unavailable. The old code recorded the same
frames as "no text on screen" and cached that answer against those bytes
for ever.

## What is still unverified

Listed in the QA matrix as "not run", and repeated in the release notes.

## After: the same fixtures on the release candidate

(filled from each engineer's report as it lands)

## QA matrix outcomes

(see QA-MATRIX.md; filled by the QA pass)

## After: the same fixtures on the release candidate

(filled after deploy to the test endpoint)
