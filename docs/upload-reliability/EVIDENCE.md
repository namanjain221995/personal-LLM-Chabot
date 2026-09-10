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

## Implementation runs

(filled from each engineer's report as it lands)

## QA matrix outcomes

(see QA-MATRIX.md; filled by the QA pass)

## After: the same fixtures on the release candidate

(filled after deploy to the test endpoint)
