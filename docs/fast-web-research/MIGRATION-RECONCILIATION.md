# Migration reconciliation — the orphaned V21

Baseline `dev` @ `29aa0ab`. Nothing committed. **Nothing applied to production yet.**

## The problem

The live database recorded a migration **21, applied 2026-09-05 04:59:59Z**, whose DDL was not in
the restored source tree. The Phase 5 restoration reverted the code; a migration cannot be
un-applied by restoring source, so the schema objects stayed behind while their code left.

Confirmed, not assumed:
* the running image (`sf-local-ai-orchestrator:cuda`, sha `5b38c191`) contains **no** `_MIGRATION_V21`
  and no `EXTRACT_VERSION` — it is the restored original, stopping at V20;
* it does **not** bind-mount app code, so edits in this checkout were never live;
* all 2,208 live rows therefore sit at the column default `0` — nothing has ever written it.

The danger was never the column. `init_schema` skips any version already recorded
(`db.py:1427`), so a **new** migration numbered 21 would have been silently skipped in
production — reporting success while applying nothing. This phase's refresh backfill was
originally numbered 21.

## Recovering the definition rather than inventing it

`git log --all -S _MIGRATION_V21` found it in commits `34a7e3c` and `829f17c` (identical in both):

```sql
ALTER TABLE web_pages ADD COLUMN IF NOT EXISTS
    extract_version smallint NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_web_pages_extract_version
    ON web_pages (extract_version) WHERE extract_version < 2;
```

**Recovering it mattered: a column-level schema diff had missed the index.** Its stated purpose —
extraction was improved, so pages stored by the older extractor hold inferior text and the vector
chunks built from it, and should be re-read in priority order rather than by mass recrawl — is
exactly the situation this phase's C2/K4 extraction fix creates again. So the column is adopted
deliberately, not dropped.

## The reconciliation

* **Identifier 21 is NOT retired — this bullet was wrong and is corrected (2026-09-07).** It
  previously read "Identifier 21 is retired permanently … fresh databases simply never have a 21;
  the tests assert that gap." The shipped code does the opposite, and the code is right: the
  authentic V21 definition was recovered from commits `34a7e3c`/`829f17c` and restored verbatim at
  `orchestrator/app/db.py:1151`, and it is registered in the migration list at `db.py:1474`, so the
  numbering is contiguous 1..27 with no gap. The test named in the old bullet asserts the opposite
  of what the bullet claimed —
  `orchestrator/tests/test_crawl_durability.py:744::test_no_migration_identifier_is_used_twice`
  asserts a *contiguous, sorted, unique* list, which a permanent gap at 21 would fail. Verified
  against the running database on 2026-09-07: `schema_migrations` holds 1..27 with no gaps, no
  defined-but-unapplied versions and no applied-but-undefined ones. The identifier is still never
  *reused* for new work — V22 onward continue past it — which is the part of the original intent
  that survives.
* **V22** — forward reconciliation. Idempotent `ADD COLUMN IF NOT EXISTS`, so it is a no-op on a
  database that ran the old V21 and creates the column on a fresh one.
* **The index is replaced, not preserved.** The original predicate hardcodes the then-current
  extractor version (`< 2`); at `EXTRACT_VERSION = 3` the pages sitting at 2 need re-reading and
  would not be in it. `idx_web_pages_extract_stale (extract_version, retrieval_count DESC)` carries
  no version literal and matches the query the worker runs.
* **V23** — the `next_refresh_at` backfill (unchanged in substance, renumbered from 22).
* `upsert_web_page` writes `extract_version` with
  `GREATEST(web_pages.extract_version, EXCLUDED.extract_version)` so it can never downgrade and
  re-queue a page forever.

## Verification — performed before applying anything to production

**The recovery procedure was itself broken, and that is why it was tested first.** The production
dump would not restore onto the test server: production runs **PostgreSQL 18.4**, that server runs
**16.15**, and `pg_restore` refused with *"unsupported version (1.16) in file header"*. The
rehearsal was redone on the version-matched isolated instance (`techsara-pg18-audit`, 55433).
**Any real restore drill must use an 18.x instance; the 16.15 server cannot restore these dumps.**

Backup taken first, outside the repo at mode 700 because it contains private conversations and
uploads: `/home/techsphere/backups/techsara-fastweb-20260906/` — 33 MB custom-format dump, a
schema-only snapshot, and the migration ledger.

| check | result |
| --- | --- |
| backup restores cleanly onto a version-matched instance | yes, no errors |
| restored copy is faithfully the live state | max_version=21, 2208 pages, 450 conversations, 1856 messages, 138 uploads |
| **1,602 stranded pages confirmed on real production data** | independently reproduces the K1 finding |
| upgrade path applies 22 and 23, preserves 21 | versions 1-23 with 21 intact |
| backfill executed | stranded 1,602 → **0** |
| backfill is staggered, not a thundering herd | 1,734 rows inside the 24h window; 474 keep the worker's own longer TTLs |
| unrelated data preserved | 2208 / 450 / 1856 / 138 all unchanged |
| `extract_version` values preserved | all 2,208 still `0` — nothing invented |
| index reconciled | old dropped, new present |
| fresh install | versions 1-20, 22, 23; **no 21** |
| **both paths converge** | schemas byte-identical (775 lines) apart from pg_dump's per-run `\restrict` nonce |
| repeated initialization | `init_schema` x3 stable, no re-application |
