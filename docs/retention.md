# Chat retention: what is deleted, when, and how to undo it

Added 2026-11-25 (Salesforce memory design v2, §12.1-§12.2). Off until the
owner turns it on (`CHAT_RETENTION_ENABLED=true`), and then a **dry run**
until the owner has read the counts (`CHAT_RETENTION_DRY_RUN=false` is a
separate, later step). With it off nothing changes.

| Piece | Where |
|---|---|
| The job | `orchestrator/app/retention.py`, `run_chat_retention()` (blocking, returns counts) and `sweep()` (the async entry for the maintenance loop) |
| Where it runs | the platform maintenance loop, `_API_PLATFORM_SWEEPS` in `orchestrator/app/main.py`, every 30 minutes; real work once per business day (Asia/Kolkata) per process. Wired in stage 2: `run_chat_retention` in main.py, and the settings below in `config.py`, `docs/CONFIG.md` and `.env.example`. With the settings at their defaults it is disabled. |
| Tests | `orchestrator/tests/test_retention.py` (test PostgreSQL) |

---

## The approved periods (owner, 2026-11-25)

| Data | Kept | Deleted by |
|---|---|---|
| Chats and their messages | until the person deletes the chat | the person (no change) |
| Salesforce query state and its summary (`sf_query_state`, V45) | with its chat | the chat's and the user's foreign keys (`ON DELETE CASCADE`). The job also removes a row past its own `expires_at`; today every row has `expires_at = NULL`, so none expires |
| `query_traces` and their `query_trace_events` | **180 days**, and never longer than their chat. Longer only when `CHAT_REQUESTS_RETENTION_DAYS` is set past 180: a trace then waits for its request (see the stamp below) | the job; the chat delete already removes them (`_SIDE_TABLES` in db.py) |
| `chat_requests` | **30 days**, and never longer than their chat | the job (terminal statuses only, see below); the chat delete already removes them |
| `ask_log.jsonl` (sf_assistant) | 90 days, one file per day | sf_assistant, not this job (design §12.3) |
| The pre-prune backup below | **30 days** (D11) | the operator, one command below |
| Whole-database backups (`scripts/backup-knowledge.sh`) | **30 days** (B1, the recommended default) | the operator, one command below |

## What the job deletes

Every cut-off uses the **database clock** (`now()`), never the orchestrator's.

| Item (count key) | Rule |
|---|---|
| `query_traces` | `started_at` more than `QUERY_TRACES_RETENTION_DAYS` (180) days ago, **and** no request of the same generation is still within its own period in a chat that still exists. That second condition holds a trace back only when `CHAT_REQUESTS_RETENTION_DAYS` is set past the trace period (see the stamp below). The trace's events go with it by the foreign key's cascade. |
| `query_traces_orphans` | the trace's chat has no `conversations` row any more, the trace is more than 1 day old, its key is **not** a bare-API key (`u<id>-…`), and it has **no** `test_case_id`. This catches chats removed when their user was deleted: the traces' `user_id` is `ON DELETE SET NULL`, so they stay behind. There is no foreign key on `conversation_id` on purpose (D7): a bare API call has no conversations row. |
| `chat_requests` | `created_at` more than `CHAT_REQUESTS_RETENTION_DAYS` (30) days ago **and** status `completed`, `failed` or `cancelled`. |
| `chat_requests_orphans` | the request's chat is gone, more than 1 day old, status terminal (as above), and not a bare-API key. |
| `chat_requests_resumable_old` | **counted and logged, never deleted** (D8): a request older than 30 days in a resumable status (`interrupted`, `accepted`, `running`, `queued`, main.py `_RESUMABLE_STATUSES`). Deleting them needs the owner's word. The log line is a warning while the count is not 0. There were 0 such rows on 2026-11-25. |
| `sf_query_state_expired` | `expires_at < now()`. Skipped cleanly while the table does not exist (before V45). |
| `old_route_sf_conversation_state`, `old_route_sf_intents`, `old_route_sf_clarifications` | the older Salesforce route's rows whose chat is gone, more than 1 day old, not a bare-API key. **Not in the approved list** (D9): deleted only when `CHAT_RETENTION_OLD_ROUTE_ORPHANS=true`, which is off by default. A dry run always counts them. |

The rules do not overlap: an orphan older than the period belongs to the
period rule, so each row is counted by exactly one item. The request items
run before the trace items (see the stamp below).

**How it deletes.** At most `CHAT_RETENTION_BATCH` rows per statement, by
`ctid`, one transaction per batch, until a batch comes back short or
`CHAT_RETENTION_MAX_BATCHES` batches have run; the next tick (30 minutes
later) goes on. One runner at a time: the run holds a session-level
PostgreSQL advisory lock on its own connection for the whole run, so a second
orchestrator (a deploy briefly runs two) skips its tick. The job never raises;
a rule that fails is logged and the others still run.

## What the job stamps before it deletes (review DS2-1)

**Why.** The toggle-off guard (`app/sf_isolation.py`) hides Salesforce
answers from turns asked with Salesforce off. It knows an answer is one by
its meta (`mode` is `salesforce`, `engine` is `sf_assistant`, or a
`salesforce_*` key), or by a server row: the `query_traces` row of the
answer's generation (`requested_mode`), or, for a generation with no trace,
its `chat_requests` row (`request->>'mode'`). `sf_isolation.sf_answer_sql`
is that rule in SQL; `db.salesforce_chat_state` applies the same rule to
each row, and it and `db.salesforce_chat_ids` also count a whole chat as
Salesforce while a server row of the chat says so. Answers stored before
the guard (2026-11-25) by a failure record, a detached answer or a tab's
copy of a stopped answer have no `mode` in their meta, so the server row is
their only Salesforce mark. Deleting it would make the guard pass such an
answer into toggle-off prompts, the chat summary, chunks and cross-chat
recall.

**What.** Each batch that deletes `chat_requests` or `query_traces` rows is
one statement, and so one transaction. It fixes the batch's rows, then
merges `{"mode": "salesforce"}` into the meta (`meta || '{"mode":
"salesforce"}'`) of every message that meets all of these conditions, and
then deletes the rows:

- it is an assistant row whose `generation_id` is the generation of one of
  the batch's rows, in whatever chat it is stored (see case (f) below);
- the guard reads it as a Salesforce answer through the server row
  (`sf_answer_sql`). For a request row that is only when the generation has
  no trace: while a trace stands, the trace is the mark, and the trace
  batch stamps the answer when the trace goes;
- its meta has no `mode` (missing or null);
- nothing else in its meta already says Salesforce;
- its meta is an object, or empty (SQL `NULL` or JSON `null`, which the
  stamp turns into `{"mode": "salesforce"}`).

Nothing else in the message changes: not the content, not any other meta
key, not `updated_at`, and never a message whose meta has a mode (an
answer stored as `assistant` stays `assistant`). For the guard, the result
is the same as before the run: it hides what it hid before, apart from the
cases listed under "What it cannot reach": the answer, its question and the
chat's place among the user's Salesforce chats. Two readers outside the
guard do see the new key: the Salesforce analytics and the admin
conversation view (see "Side effects" at the end of this page). The rows
still go at the approved periods.

The request items run before the trace items. The guard reads a request
only when there is no trace, so with the traces still in place every answer
is stamped by the row the guard actually reads. In the other order, a run
that deletes an old trace and its old request together (the first real run)
would hand the answer to the request halfway through and could stamp an
answer the guard did not read as Salesforce before (a member without
Salesforce who asked in Salesforce mode was answered in assistant mode).

For the same reason a trace does not go while its request still stands in
a chat that still exists (review R1-1):

- when a request item fails or stops at the batch cap, the run deletes no
  trace. It logs `retention: held back query_traces,query_traces_orphans:
  …`, and the result has no key for those two items. The failure or the
  cap leaves the day open, so the next tick (30 minutes later) runs the
  requests and then the traces;
- when `CHAT_REQUESTS_RETENTION_DAYS` is set past the trace period, a trace
  whose request is still within that period waits, and goes in the run
  that deletes its request, after it. No request item deletes such a
  request, so the dry run still counts what the real run deletes.

The one exception is a request the job keeps for good (a resumable one,
D8); see "What it cannot reach".

**Counts.** The dry run counts the messages it would stamp with the same
text (`messages_stamped=N` on its line, gauge
`retention_candidates{table="messages_stamped"}`), and stamps nothing.
When one of the four items whose rows mark answers fails in a dry run, its
line says `messages_stamped=unknown` instead, the result has no
`messages_stamped` key and the gauge is not set: a number without that
item's answers would read as the whole count (review R2-4). A real run
returns `messages_stamped`, logs `retention: stamped messages_stamped=N
(...)` when N is not 0, and raises `retention_stamped_total{table="<item>"}`
by the answers each item stamped.

**What it cannot reach.** The stamp cannot mark these four, so once their
server row goes they carry no Salesforce mark:

- (a) a Salesforce answer that no server row names by its generation: one
  stored with no `generation_id` (an old detached answer whose meta had
  none), or an earlier attempt whose request now names the retry's
  generation. The guard hides it only because a server row of the same chat
  says Salesforce, which marks the chat, not the answer;
- (b) a question asked in Salesforce mode whose answer was never stored.
  The guard knows it only through its request's `intent_id`, and it does
  not read a question's meta, so there is nothing to stamp. The fix belongs
  in the guard (`db.salesforce_chat_state` reading a question's own mode),
  not in this job;
- (c) an answer whose meta has a mode other than `salesforce` (for example
  `assistant`) although its server row says Salesforce. Only a client could
  have written that (toggle-off review F5), and a mode is never
  overwritten;
- (d) an answer whose meta is a JSON array, string, number or boolean
  instead of an object (review R1-4). The guard reads such a meta as empty
  and lets the server row decide, but merging a key into it would not be
  additive, so it is never stamped. To count these answers before the first
  real run: `SELECT count(*) FROM messages WHERE role = 'assistant' AND
  jsonb_typeof(meta) NOT IN ('object', 'null');`

What happens next depends on the rest of the chat.

*The whole chat goes clean* when no answer of the chat keeps a mark. While
a server row of a chat says Salesforce, the guard counts the whole chat as
holding Salesforce. It then also hides the chat's other answers that it
cannot show to be clean (for example every later answer without the
guard's clean stamp), and the chat is one of the user's Salesforce chats,
which cross-chat recall and the embedder leave out. When the chat's last
such row goes and no answer of the chat says Salesforce in its meta
(written before the run, or by the stamp), the guard reads the chat as
clean. From then on all of it reaches toggle-off prompts, the chat summary,
chunks and cross-chat recall: the cases above and the other answers that
were hidden only because the chat held Salesforce, and the chat leaves the
user's Salesforce set. **Each of (a) to (d) does this on its own** when it
is the chat's only Salesforce answer (review R2-1). To count those chats
before the first real run (read-only; it counts each chat that holds
Salesforce through a server row today and would hold nothing once all its
server rows are gone; a chat with a Salesforce request in a resumable
status keeps holding, because the job keeps that request, D8):

<!-- retention-count-whole-chats -->
```sql
SELECT count(*) AS chats
  FROM conversations c
 WHERE (EXISTS (SELECT 1 FROM query_traces t
                 WHERE t.conversation_id = c.id AND t.requested_mode = 'salesforce')
        OR EXISTS (SELECT 1 FROM chat_requests r
                    WHERE r.conversation_id = c.id AND r.request->>'mode' = 'salesforce'
                      AND NOT EXISTS (SELECT 1 FROM query_traces t2 WHERE t2.trace_id = r.generation_id)))
   AND NOT EXISTS (SELECT 1 FROM chat_requests k
                    WHERE k.conversation_id = c.id AND k.request->>'mode' = 'salesforce'
                      AND k.status IN ('interrupted', 'accepted', 'running', 'queued'))
   AND NOT EXISTS (
         SELECT 1 FROM messages m
          WHERE m.conversation_id = c.id AND m.role = 'assistant'
            AND (COALESCE(jsonb_path_exists(m.meta, '$ ? (@.mode == "salesforce" || @.engine == "sf_assistant" || exists(@.salesforce_mode) || exists(@.salesforce_sources) || exists(@.salesforce_scope))'), false)
                 OR ((m.meta IS NULL OR jsonb_typeof(m.meta) IN ('object', 'null'))
                     AND m.meta->>'mode' IS NULL
                     AND CASE WHEN EXISTS (SELECT 1 FROM query_traces x WHERE x.trace_id = m.generation_id)
                              THEN EXISTS (SELECT 1 FROM query_traces x WHERE x.trace_id = m.generation_id
                                            AND x.requested_mode = 'salesforce')
                              ELSE EXISTS (SELECT 1 FROM chat_requests y WHERE y.generation_id = m.generation_id
                                            AND y.request->>'mode' = 'salesforce') END)));
```
<!-- /retention-count-whole-chats -->

The last `NOT EXISTS` is every answer that keeps a mark: one whose meta
says Salesforce already, or one the stamp will reach. A count that instead
looks for any answer a server row names by its generation misses the chats
whose only Salesforce answers are of kind (c) or (d), because those answers
are named but never stamped.

*One message at a time* when another answer of the chat keeps its mark (a
stamped answer, or one whose meta said Salesforce before the run). The chat
still holds Salesforce, and the guard still hides (a), (d), and a (c)
answer stored after the chat's first marked answer, as answers it cannot
show to be clean. Two still reach toggle-off turns:

- a (c) answer stored before the chat's first marked answer: the guard
  reads it as an earlier assistant-mode answer;
- the question of (b): once its request is gone, the guard reads it as the
  person's own words.

Two more cases differ from what the guard did before the run, the other
way round: they are hidden from then on, more and never less.

- (e) an answer whose trace goes at its period while its request stays,
  because the request is in a resumable status and older than the request
  period (counted and kept, D8). The guard then reads that request. When
  the trace said `assistant` and the request asked for Salesforce, the
  answer is hidden from then on. There were 0 such requests on 2026-11-25.
- (f) an answer stored in a chat other than the deleted row's, under that
  row's generation (review R2-3). The stamp matches the generation in any
  chat, as the guard's per-row reading does. The guard reads a chat's rows
  only while a server row of that chat, or an answer's meta, says
  Salesforce, so it may have shown such a copy; after the stamp it hides
  it, and the chat joins the user's Salesforce set. This is deliberate: the
  copy is of a Salesforce answer. No code path is known to store one
  generation in two chats (`idx_messages_generation` is unique per chat).

**Rollback.** Stopping the job stops the stamp too. The stamp records in
the answer what the deleted row said, so for the guard nothing needs
undoing: no guard decision changes, apart from the cases above. Rows
brought back from a backup (below) leave the stamp in place, which is
harmless. The stamped answers are not listed anywhere; the counts above are
the record. The pre-prune backup holds no `messages`, so it cannot undo a
stamp; only a whole-database backup taken before the run (B1) holds the
messages as they were.

## What is never deleted

- Chats and messages, conversation summaries, uploads, documents, artifacts,
  facts and everything else not in the table above. The only write to a
  message is the stamp above: one meta key on an old Salesforce answer,
  never its text.
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
request body. The same holds for the stamp.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `CHAT_RETENTION_ENABLED` | `false` | runs the job at all |
| `CHAT_RETENTION_DRY_RUN` | `true` | counts only, deletes nothing |
| `QUERY_TRACES_RETENTION_DAYS` | `180` | trace period (never below 180) |
| `CHAT_REQUESTS_RETENTION_DAYS` | `30` | request period (never below 30). Set past the trace period, a trace also waits for its request |
| `CHAT_RETENTION_BATCH` | `1000` | rows per delete statement (1-10,000) |
| `CHAT_RETENTION_MAX_BATCHES` | `20` | batches per item per run (1-1,000) |
| `CHAT_RETENTION_OLD_ROUTE_ORPHANS` | `false` | D9: also delete the older route's orphan rows. Needs the owner's word. |
| `CHAT_RETENTION_BACKUP_DATE` | empty | the date (`YYYY-MM-DD`) of the pre-prune backup; while it is more than 30 days old, each daily run logs a warning to delete it |

Each change needs an orchestrator restart.

## The dry run

With `CHAT_RETENTION_ENABLED=true` and the dry run on, the first tick after
start (about 60 seconds) and then one tick a day writes one line:

```
retention dry-run: chat_requests=N chat_requests_orphans=N chat_requests_resumable_old=N query_traces=N query_traces_orphans=N sf_query_state_expired=N old_route_sf_conversation_state=N old_route_sf_intents=N old_route_sf_clarifications=N messages_stamped=N
```

(Before V45 there is no `sf_query_state_expired=N`; the line ends with
`skipped=sf_query_state_expired` instead.)

and sets the gauge `retention_candidates{table="<item>"}` per item. Read it
with `docker logs <orchestrator container> 2>&1 | grep 'retention dry-run'`.
The count uses the same `WHERE` as the delete (one rule text feeds both), and
a test proves that the counts equal what a real run then deletes.
`messages_stamped` is the number of answers the real run would stamp before
it deletes (see above); a test proves it equals what the real run stamps.
When the line says `messages_stamped=unknown`, an item failed (its warning
is in the log just before); wait for the next day's line, or run the dry
run again.

The owner reads the counts, and the two read-only counts under "What it
cannot reach" (answers of kind (d), and chats that would go clean whole).
Only then: the backup, then the real run.

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
`retention: deleted chat_requests=N …` (what it removed) and
`retention: counted, kept chat_requests_resumable_old=N` (requests in a
resumable status, counted and never deleted, D8), and a third,
`retention: stamped messages_stamped=N (…)`, when it stamped answers. The
counter `retention_deleted_total{table="<item>"}` rises by what it removed.
If the backlog is larger than one run takes (`CHAT_RETENTION_BATCH` ×
`CHAT_RETENTION_MAX_BATCHES` rows of requests, 20,000 by default), it also
logs `retention: held back query_traces,query_traces_orphans: …`: the
traces go on a later tick, after the remaining requests.

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
backup. Since 2026-11-25 the script sets `umask 077` itself, whatever the
caller's umask, so it writes every new backup folder as 700 and every new
file as 600; run it as `scripts/backup-knowledge.sh`, with no wrapper. It
does not change what already exists: a backups folder created before then
keeps its mode, and so does every dump in it, which under the usual login
umask 0002 is 0775 for folders and 0664 for files, readable by every local
account. Make those private once:

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
  meta remains. An earlier version of this page said that a turn with no
  mode then counts as Salesforce; it does not, and once its server row is
  gone such an answer would reach toggle-off turns (review DS2-1). That is
  why the job stamps `meta.mode = 'salesforce'` on those answers in the same
  transaction as the delete (see "What the job stamps before it deletes"),
  so the guard hides what it hid before. The cases the stamp cannot reach
  are listed there.
- For a chat whose last send is older than 30 days, "the previous send" reads
  as none, which the code already handles as "no earlier send".
- `/admin/analytics/salesforce` no longer counts traces older than 180 days.
- The stamp changes past numbers on `/admin/analytics/salesforce` (review
  R2-2). Its answer totals and its series count an assistant message as a
  Salesforce answer when its meta says `mode` is `salesforce` (or `route` is
  `sql` or `dataset`), in the window of the message's own `created_at`. A
  failure record or a stopped answer's copy stored before the guard had
  neither key, so it was not counted; once the first real run stamps it, it
  is, so a window in the past gains up to `messages_stamped` answers. The
  admin conversation view (`/admin/members/<id>/conversations/<cid>`) labels
  a message by `meta.mode` first, so a stamped answer now shows as
  `salesforce`. A client's later history sync keeps the stamp, because the
  server owns `mode` (`db._carry_isolation_meta`, toggle-off review F5).
- A browser re-sending an intent older than 30 days is treated as a new send.
