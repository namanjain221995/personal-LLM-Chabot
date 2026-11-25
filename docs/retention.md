# Chat retention: what is deleted, when, and how to undo it

Added 2026-11-25 (Salesforce memory design v2, §12.1-§12.2). Off until the
owner turns it on (`CHAT_RETENTION_ENABLED=true`), and then a **dry run**
until the owner has read the counts (`CHAT_RETENTION_DRY_RUN=false` is a
separate, later step). With it off nothing changes.

| Piece | Where |
|---|---|
| The job | `orchestrator/app/retention.py`, `run_chat_retention()` (blocking, returns counts) and `sweep()` (the async entry for the maintenance loop) |
| Where it runs | the platform maintenance loop, `_API_PLATFORM_SWEEPS` in `orchestrator/app/main.py`, every 30 minutes; real work once per business day (Asia/Kolkata) per process. **Not wired yet**: stage 2 adds the sweep name to main.py and the settings below to `config.py`, `docs/CONFIG.md` and `.env.example`. Until then the module reads every setting with its default, so it is disabled. |
| Tests | `orchestrator/tests/test_retention.py` (test PostgreSQL) |

---

## The approved periods (owner, 2026-11-25)

| Data | Kept | Deleted by |
|---|---|---|
| Chats and their messages | until the person deletes the chat | the person (no change) |
| Salesforce query state and its summary (`sf_query_state`, V45) | with its chat | the chat's and the user's foreign keys (`ON DELETE CASCADE`). The job also removes a row past its own `expires_at`; today every row has `expires_at = NULL`, so none expires |
| `query_traces` and their `query_trace_events` | **180 days**, and never longer than their chat | the job; the chat delete already removes them (`_SIDE_TABLES` in db.py) |
| `chat_requests` | **30 days**, and never longer than their chat | the job (terminal statuses only, see below); the chat delete already removes them |
| `ask_log.jsonl` (sf_assistant) | 90 days, one file per day | sf_assistant, not this job (design §12.3) |
| The pre-prune backup below | **30 days** (D11) | the operator, one command below |
| Whole-database backups (`scripts/backup-knowledge.sh`) | **30 days** (B1, the recommended default) | the operator, one command below |

## What the job deletes

Every cut-off uses the **database clock** (`now()`), never the orchestrator's.

| Item (count key) | Rule |
|---|---|
| `query_traces` | `started_at` more than `QUERY_TRACES_RETENTION_DAYS` (180) days ago. The trace's events go with it by the foreign key's cascade. |
| `query_traces_orphans` | the trace's chat has no `conversations` row any more, the trace is more than 1 day old, its key is **not** a bare-API key (`u<id>-…`), and it has **no** `test_case_id`. This catches chats removed when their user was deleted: the traces' `user_id` is `ON DELETE SET NULL`, so they stay behind. There is no foreign key on `conversation_id` on purpose (D7): a bare API call has no conversations row. |
| `chat_requests` | `created_at` more than `CHAT_REQUESTS_RETENTION_DAYS` (30) days ago **and** status `completed`, `failed` or `cancelled`. |
| `chat_requests_orphans` | the request's chat is gone, more than 1 day old, status terminal (as above), and not a bare-API key. |
| `chat_requests_resumable_old` | **counted and logged, never deleted** (D8): a request older than 30 days in a resumable status (`interrupted`, `accepted`, `running`, `queued`, main.py `_RESUMABLE_STATUSES`). Deleting them needs the owner's word. The log line is a warning while the count is not 0. There were 0 such rows on 2026-11-25. |
| `sf_query_state_expired` | `expires_at < now()`. Skipped cleanly while the table does not exist (before V45). |
| `old_route_sf_conversation_state`, `old_route_sf_intents`, `old_route_sf_clarifications` | the older Salesforce route's rows whose chat is gone, more than 1 day old, not a bare-API key. **Not in the approved list** (D9): deleted only when `CHAT_RETENTION_OLD_ROUTE_ORPHANS=true`, which is off by default. A dry run always counts them. |

The rules do not overlap: an orphan older than the period belongs to the
period rule, so each row is counted by exactly one item.

**How it deletes.** At most `CHAT_RETENTION_BATCH` rows per statement, by
`ctid`, one transaction per batch, until a batch comes back short or
`CHAT_RETENTION_MAX_BATCHES` batches have run; the next tick (30 minutes
later) goes on. One runner at a time: the run holds a session-level
PostgreSQL advisory lock on its own connection for the whole run, so a second
orchestrator (a deploy briefly runs two) skips its tick. The job never raises;
a rule that fails is logged and the others still run.

## What is never deleted

- Chats and messages, conversation summaries, uploads, documents, artifacts,
  facts and everything else not in the table above.
- A trace or request of a chat that still exists, before its period ends.
- A request in a resumable status, however old (D8).
- A bare API call's traces and requests before their 180 or 30 days, and a
  trace with a `test_case_id` before its 180 days.
- Anything less than 1 day old by the orphan rules.
- The older route's rows, unless D9 is turned on.
- A period shorter than the approved one: a lower setting is raised to the
  approved value with a warning in the log. A setting can only keep data
  longer.

The job writes counts to the log, never content: no question, answer, id or
request body.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `CHAT_RETENTION_ENABLED` | `false` | runs the job at all |
| `CHAT_RETENTION_DRY_RUN` | `true` | counts only, deletes nothing |
| `QUERY_TRACES_RETENTION_DAYS` | `180` | trace period (never below 180) |
| `CHAT_REQUESTS_RETENTION_DAYS` | `30` | request period (never below 30) |
| `CHAT_RETENTION_BATCH` | `1000` | rows per delete statement (1-10,000) |
| `CHAT_RETENTION_MAX_BATCHES` | `20` | batches per item per run (1-1,000) |
| `CHAT_RETENTION_OLD_ROUTE_ORPHANS` | `false` | D9: also delete the older route's orphan rows. Needs the owner's word. |
| `CHAT_RETENTION_BACKUP_DATE` | empty | the date (`YYYY-MM-DD`) of the pre-prune backup; while it is more than 30 days old, each daily run logs a warning to delete it |

Each change needs an orchestrator restart.

## The dry run

With `CHAT_RETENTION_ENABLED=true` and the dry run on, the first tick after
start (about 60 seconds) and then one tick a day writes one line:

```
retention dry-run: query_traces=N query_traces_orphans=N chat_requests=N chat_requests_orphans=N chat_requests_resumable_old=N sf_query_state_expired=N old_route_sf_conversation_state=N old_route_sf_intents=N old_route_sf_clarifications=N
```

(Before V45 there is no `sf_query_state_expired=N`; the line ends with
`skipped=sf_query_state_expired` instead.)

and sets the gauge `retention_candidates{table="<item>"}` per item. Read it
with `docker logs <orchestrator container> 2>&1 | grep 'retention dry-run'`.
The count uses the same `WHERE` as the delete (one rule text feeds both), and
a test proves that the counts equal what a real run then deletes.

The owner reads the counts. Only then: the backup, then the real run.

## Before the first real run: the backup

It is taken inside the postgres container over its socket, the way
`scripts/backup-knowledge.sh` does it, so no password is read or typed. It
holds real questions and transcripts, so it is private: the folder is 700 and
the file 600.

**Run this block once, before the first real run, and never again after
it.** The dump is the only copy of the rows the job deletes; one taken after
a real run no longer holds them. The block therefore refuses to write over a
dump that is already there (it checks first, and `noclobber` makes the
redirect itself fail too). To look at the backup again, use the `ls` and
`grep` lines alone.

```bash
(
  set -eu
  set -o noclobber   # never overwrite: this dump is the only rollback copy
  umask 077
  day="$(date +%F)"
  dir="$HOME/Documents/project/backups/retention_$day"
  if [ -e "$dir/retention_tables.sql" ]; then
    echo "refusing: $dir/retention_tables.sql already exists; it is the pre-prune copy, keep it" >&2
    exit 1
  fi
  mkdir -p "$dir"
  chmod 700 "$dir"
  docker exec sf-local-ai-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --data-only -t query_traces -t query_trace_events -t chat_requests' > "$dir/retention_tables.sql"
  chmod 600 "$dir/retention_tables.sql"
  ls -ld "$dir"; ls -l "$dir/retention_tables.sql"
  grep -c '^COPY ' "$dir/retention_tables.sql"    # 3: one per table
  echo "CHAT_RETENTION_BACKUP_DATE=$day"
)
```

If the dump itself failed (the block stopped with an error, or the `COPY`
count is not 3), the file is incomplete. Before any real run has happened,
delete that file and run the block again; the refusal above is only for a
complete pre-prune dump.

If D9 is turned on as well, add `-t sf_conversation_state -t sf_intents -t
sf_clarifications` to the `pg_dump` line.

Then set `CHAT_RETENTION_BACKUP_DATE` to the date it printed and
`CHAT_RETENTION_DRY_RUN=false`, and restart the orchestrator. The first tick
after the restart (about 60 seconds) deletes. It logs two lines:
`retention: deleted query_traces=N …` (what it removed) and
`retention: counted, kept chat_requests_resumable_old=N` (requests in a
resumable status, counted and never deleted, D8). The counter
`retention_deleted_total{table="<item>"}` rises by what it removed.

## Deleting the backup after 30 days (D11)

The orchestrator cannot see the host folder, so it only reminds: while
`CHAT_RETENTION_BACKUP_DATE` is more than 30 days old, each daily run logs a
warning. The operator deletes it with one command, looks at the list first:

```bash
find "$HOME/Documents/project/backups" -maxdepth 1 -type d -name 'retention_*' -mtime +30 -print
find "$HOME/Documents/project/backups" -maxdepth 1 -type d -name 'retention_*' -mtime +30 -exec rm -rf {} +
```

and then clears `CHAT_RETENTION_BACKUP_DATE`.

## Whole-database backups (B1)

`scripts/backup-knowledge.sh` dumps the **whole** database into
`backups/<stamp>/` under the repository root (or the folder given with
`--dest`, or `BACKUP_DEST`) and never deletes an earlier backup. Any such
dump keeps deleted chats, their Salesforce state and the traces and requests
this job removed, for as long as the dump exists. Recommended (B1, applied
here as the default): keep them **30 days**, the same as the pre-prune
backup, then delete them.

Set `BACKUP_DEST` to the backups folder first: the commands refuse to run
without it. Only folders that carry the script's `manifest.json` are
touched (the script itself marks its backups that way and refuses any other
folder), so nothing else in that folder can match. Look at the list first:

```bash
export BACKUP_DEST="$PWD/backups"   # run from the repository root; or the folder given to --dest
find "${BACKUP_DEST:?set BACKUP_DEST to the backups folder}" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +30 -exec sh -c 'test -f "$1/manifest.json"' sh {} ';' -print
find "${BACKUP_DEST:?set BACKUP_DEST to the backups folder}" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +30 -exec sh -c 'test -f "$1/manifest.json"' sh {} ';' -exec rm -rf {} +
```

**Modes.** These dumps hold every conversation, so they must be private:
folders 700 (`drwx------`) and files 600 (`-rw-------`), like the pre-prune
backup. The script does not set its own umask yet (that is a separate change
to the script), so under the usual login umask 0002 it writes the dump as
0664 and its folders as 0775, readable by every local account. Until the
script is changed, run it as `(umask 077; scripts/backup-knowledge.sh)`, and
make the dumps already there private once:

```bash
export BACKUP_DEST="$PWD/backups"   # run from the repository root; or the folder given to --dest
chmod -R go-rwx "${BACKUP_DEST:?set BACKUP_DEST to the backups folder}"
ls -la "$BACKUP_DEST"
```

The privacy document must say that backups keep deleted chats until they are
deleted.

## Rollback

**Stop deleting:** `CHAT_RETENTION_ENABLED=false` (or back to
`CHAT_RETENTION_DRY_RUN=true`) and restart the orchestrator.

**Bring deleted rows back** from the pre-prune backup, or from a
whole-database backup taken before the run (B1); nothing else holds them.
The steps below use the pre-prune backup. The restore puts back only rows
whose chat still exists **and still belongs to the row's user**, or a
bare-API key (`u<id>-…`) of the row's own user while that user exists: a
chat or a user deleted since the backup stays deleted, and a chat id that
another person has claimed since then gets nothing of the earlier owner's
(conversation ids are chosen by the client). A trace whose user was deleted
(its `user_id` became NULL) is not put back. Rows that are still there are
skipped. Run each block
with `docker exec -i sf-local-ai-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'`
and the block on standard input. Try it on a copy of the database first.

1. Empty staging tables with the live columns:

<!-- retention-restore-staging -->
```sql
CREATE SCHEMA retention_restore;
CREATE TABLE retention_restore.query_traces (LIKE public.query_traces);
CREATE TABLE retention_restore.query_trace_events (LIKE public.query_trace_events);
CREATE TABLE retention_restore.chat_requests (LIKE public.chat_requests);
```
<!-- /retention-restore-staging -->

2. Load the backup into them. The `sed` points every `COPY` at the staging
   tables and drops the sequence line, so the live identity counter is never
   moved back:

   ```bash
   sed -e 's/^COPY public\./COPY retention_restore./' -e '/^SELECT pg_catalog.setval(/d' \
       "$HOME/Documents/project/backups/retention_<date>/retention_tables.sql" \
     | docker exec -i sf-local-ai-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q'
   ```

3. Put back what qualifies, in one transaction:

<!-- retention-restore-insert -->
```sql
BEGIN;
INSERT INTO public.query_traces
SELECT r.* FROM retention_restore.query_traces r
 WHERE EXISTS (SELECT 1 FROM public.users u WHERE u.id = r.user_id)
   AND (EXISTS (SELECT 1 FROM public.conversations c
                 WHERE c.id = r.conversation_id AND c.user_id = r.user_id)
        OR r.conversation_id LIKE ('u' || r.user_id || '-%'))
ON CONFLICT DO NOTHING;
INSERT INTO public.query_trace_events
SELECT e.* FROM retention_restore.query_trace_events e
 WHERE EXISTS (SELECT 1 FROM public.query_traces t WHERE t.trace_id = e.trace_id)
ON CONFLICT DO NOTHING;
INSERT INTO public.chat_requests
SELECT r.* FROM retention_restore.chat_requests r
 WHERE EXISTS (SELECT 1 FROM public.users u WHERE u.id = r.user_id)
   AND (EXISTS (SELECT 1 FROM public.conversations c
                 WHERE c.id = r.conversation_id AND c.user_id = r.user_id)
        OR r.conversation_id LIKE ('u' || r.user_id || '-%'))
ON CONFLICT DO NOTHING;
COMMIT;
```
<!-- /retention-restore-insert -->

4. Drop the staging tables:

<!-- retention-restore-cleanup -->
```sql
DROP SCHEMA retention_restore CASCADE;
```
<!-- /retention-restore-cleanup -->

`tests/test_retention.py` runs blocks 1, 3 and 4 exactly as written here
against the test database, including a chat id that another person claimed
after the backup. It also runs the backup block above (twice, with a
stand-in for docker), the B1 delete and the B1 mode repair on a temporary
folder. The older route's rows (D9) and expired
`sf_query_state` rows are orphans or expired by definition, so a restore by
the same rule would put none of them back.

## Side effects the approved policy accepts

- The toggle-off guard reads `chat_requests` and `query_traces` as
  server-written Salesforce signals. Past 30 and 180 days only the message
  meta remains, and a turn with no mode then counts as Salesforce: it hides
  more, never less.
- For a chat whose last send is older than 30 days, "the previous send" reads
  as none, which the code already handles as "no earlier send".
- `/admin/analytics/salesforce` no longer counts traces older than 180 days.
- A browser re-sending an intent older than 30 days is treated as a new send.
