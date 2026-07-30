# Data model — the three persistence layers

Everything the platform stores lives in the single `data` Docker volume, mounted **read-write into
both** the orchestrator ([docker-compose.yml:269](../../docker-compose.yml#L269)) and the sync worker
([:320](../../docker-compose.yml#L320)), plus a separate `reports` volume
([:270](../../docker-compose.yml#L270)).

| Layer | Path | Writer | Reader | Engine |
|---|---|---|---|---|
| **App state** | `/data/app.sqlite3` ([config.py:255](../../orchestrator/app/config.py#L255)) | orchestrator only | orchestrator only | stdlib `sqlite3`, WAL |
| **Analytics warehouse** | `/data/warehouse.duckdb` ([:245,316](../../docker-compose.yml#L245)) | sync worker only (read-write) | orchestrator (**`read_only=True`**) | DuckDB |
| **Vector index** | `/data/lancedb` ([:246,317](../../docker-compose.yml#L246)) | sync worker only | orchestrator | LanceDB |
| Batch landing zone | `/data/parquet` ([:247,318](../../docker-compose.yml#L247)) | sync worker only | **nothing** | Parquet files |
| Ephemeral workspaces | `/data/workspaces` ([config.py:214](../../orchestrator/app/config.py#L214)) | orchestrator | orchestrator | filesystem |
| Generated reports | `/reports` ([:248](../../docker-compose.yml#L248)) | orchestrator | orchestrator | filesystem |

`APP_DB_PATH`, `WORKSPACE_DIR` and `LANCEDB_TABLE` are **not** forwarded by compose, so their
`config.py` defaults are what actually apply in the container.

---

## `app.sqlite3` — application state

**Purpose** — The entire app-state persistence layer: users, conversations, messages, rolling
summaries, embedded conversation chunks, dataset uploads, fetched URLs, cloned repos and repo chunks.
Explicitly *not* the analytics data plane ([db.py:1-9](../../orchestrator/app/db.py#L1-L9)).

**Public surface** — one DDL constant and one connection factory; every accessor in the 1,064-line
module goes through them.

| Symbol | Signature | `file:line` |
|---|---|---|
| `_SCHEMA` | DDL script, 9 tables + 6 indexes | [db.py:22-134](../../orchestrator/app/db.py#L22-L134) |
| `_ADDED_CONVERSATION_COLUMNS` | `(("pinned", …), ("archived", …))` | [db.py:141-144](../../orchestrator/app/db.py#L141-L144) |
| `_ADDED_MESSAGE_COLUMNS` | `(("generation_id", "TEXT"),)` | [db.py:146](../../orchestrator/app/db.py#L146) |
| `utcnow` | `() -> str` (ISO-8601 UTC) | [db.py:149](../../orchestrator/app/db.py#L149) |
| `migrate` | `(con: sqlite3.Connection) -> None` | [db.py:153](../../orchestrator/app/db.py#L153) |
| `connect` | `() -> sqlite3.Connection` | [db.py:195](../../orchestrator/app/db.py#L195) |

### Full DDL

| Table | Columns | FK to `conversations`? | `file:line` |
|---|---|---|---|
| `users` | `id INTEGER PK AUTOINCREMENT`, `username TEXT NOT NULL UNIQUE COLLATE NOCASE`, `password_hash TEXT NOT NULL`, `created_at TEXT NOT NULL` | n/a | [db.py:23-28](../../orchestrator/app/db.py#L23-L28) |
| `conversations` | `id TEXT PK` (**client-supplied**), `user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE`, `title`, `created_at`, `updated_at`, `pinned INTEGER NOT NULL DEFAULT 0`, `archived INTEGER NOT NULL DEFAULT 0` | n/a | [db.py:29-37](../../orchestrator/app/db.py#L29-L37) |
| `messages` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE`, `role`, `content`, `meta TEXT`, `created_at`, `generation_id TEXT` | **YES — CASCADE** [:40](../../orchestrator/app/db.py#L40) | [db.py:38-51](../../orchestrator/app/db.py#L38-L51) |
| `conversation_summaries` | `conversation_id TEXT PK REFERENCES conversations(id) ON DELETE CASCADE`, `summary`, `covers_through INTEGER`, `token_estimate INTEGER`, `updated_at` | **YES — CASCADE** [:56](../../orchestrator/app/db.py#L56) | [db.py:55-65](../../orchestrator/app/db.py#L55-L65) |
| `conversation_chunks` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE`, `ordinal INTEGER`, `role`, `text`, `embedding BLOB NOT NULL`, `created_at`, `UNIQUE(conversation_id, ordinal)` | **YES — CASCADE** [:71](../../orchestrator/app/db.py#L71) | [db.py:69-78](../../orchestrator/app/db.py#L69-L78) |
| `uploads` | `id TEXT PK`, `conversation_id TEXT NOT NULL`, `filename`, `bytes INTEGER`, `status`, `profile TEXT`, `notes TEXT`, `created_at` | **NO FK** | [db.py:84-93](../../orchestrator/app/db.py#L84-L93) |
| `url_documents` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL`, `url`, `title`, `text`, `fetched_at`, `UNIQUE(conversation_id, url)` | **NO FK** | [db.py:102-110](../../orchestrator/app/db.py#L102-L110) |
| `repos` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL`, `repo_key`, `url`, `sha`, `cloned_at`, `UNIQUE(conversation_id, repo_key)` | **NO FK** | [db.py:114-122](../../orchestrator/app/db.py#L114-L122) |
| `repo_chunks` | `id INTEGER PK AUTOINCREMENT`, `conversation_id TEXT NOT NULL`, `repo_key`, `path`, `start_line INTEGER`, `end_line INTEGER`, `text` | **NO FK** | [db.py:123-131](../../orchestrator/app/db.py#L123-L131) |

### Indexes

| Index | Definition | `file:line` |
|---|---|---|
| `idx_conversation_chunks_conv` | `ON conversation_chunks(conversation_id, ordinal)` | [db.py:79-80](../../orchestrator/app/db.py#L79-L80) |
| `idx_uploads_conversation` | `ON uploads(conversation_id, created_at)` | [db.py:94-95](../../orchestrator/app/db.py#L94-L95) |
| `idx_conversations_user` | `ON conversations(user_id, updated_at DESC)` | [db.py:96-97](../../orchestrator/app/db.py#L96-L97) |
| `idx_messages_conversation` | `ON messages(conversation_id, id)` | [db.py:98-99](../../orchestrator/app/db.py#L98-L99) |
| `idx_url_documents_conv` | `ON url_documents(conversation_id, id)` | [db.py:111-112](../../orchestrator/app/db.py#L111-L112) |
| `idx_repo_chunks_conv` | `ON repo_chunks(conversation_id, repo_key, id)` | [db.py:132-133](../../orchestrator/app/db.py#L132-L133) |
| **`idx_messages_generation`** | `UNIQUE ON messages(conversation_id, generation_id) WHERE generation_id IS NOT NULL` — created in `migrate`, not `_SCHEMA` | [db.py:187-191](../../orchestrator/app/db.py#L187-L191) |

Four further indexes are implicit, created by SQLite for the `UNIQUE` constraints on
`users.username`, `conversation_chunks(conversation_id, ordinal)`,
`url_documents(conversation_id, url)` and `repos(conversation_id, repo_key)` — which makes
`idx_conversation_chunks_conv` ([db.py:79-80](../../orchestrator/app/db.py#L79-L80)) redundant with the
`UNIQUE` at [db.py:77](../../orchestrator/app/db.py#L77). **There is no index on `messages.content`**,
so the `LIKE` scans at [db.py:963,972,995](../../orchestrator/app/db.py#L963) are full table scans.

**Control flow** — `connect()` ([db.py:195-205](../../orchestrator/app/db.py#L195-L205)), executed by
**every** accessor in the module:
1. `Path(settings.app_db_path)` — [db.py:197](../../orchestrator/app/db.py#L197).
2. `path.parent.mkdir(parents=True, exist_ok=True)` — [db.py:198](../../orchestrator/app/db.py#L198). A filesystem write on every call.
3. `sqlite3.connect(str(path))` — [db.py:199](../../orchestrator/app/db.py#L199). **No `timeout=`** (stdlib default 5.0 s busy timeout) and **no `check_same_thread=False`**.
4. `con.row_factory = sqlite3.Row` — [db.py:200](../../orchestrator/app/db.py#L200).
5. **`PRAGMA journal_mode=WAL`** — [db.py:201](../../orchestrator/app/db.py#L201) — readers never block the writer.
6. **`PRAGMA foreign_keys=ON`** — [db.py:202](../../orchestrator/app/db.py#L202). SQLite defaults this **off**, and it is per-connection, so setting it here on every connection is what makes the three CASCADE clauses real.
7. `con.executescript(_SCHEMA)` — [db.py:203](../../orchestrator/app/db.py#L203) — 15 `CREATE … IF NOT EXISTS` statements, every time.
8. `migrate(con)` — [db.py:204](../../orchestrator/app/db.py#L204).

`migrate` ([db.py:153-192](../../orchestrator/app/db.py#L153-L192)) — **the additive-only migration
policy**, stated verbatim at [db.py:137-140](../../orchestrator/app/db.py#L137-L140): *"the live DB
holds the owner's real conversations, so the only permitted migration step is `ALTER TABLE … ADD
COLUMN`, and only when the column is missing — never a DROP, never a `CREATE … AS SELECT` rewrite,
never a row update."*
1. `PRAGMA table_info(conversations)`; return early if the table is absent — [db.py:161-163](../../orchestrator/app/db.py#L161-L163).
2. Conditionally `ALTER TABLE conversations ADD COLUMN pinned|archived` — [db.py:164-166](../../orchestrator/app/db.py#L164-L166). Existing rows pick up `DEFAULT 0`.
3. `PRAGMA table_info(messages)`; conditionally add `generation_id` — [db.py:167-171](../../orchestrator/app/db.py#L167-L171).
4. **Unconditional** `DELETE FROM messages WHERE generation_id IS NOT NULL AND id NOT IN (SELECT MIN(id) … GROUP BY conversation_id, generation_id)` — [db.py:181-186](../../orchestrator/app/db.py#L181-L186). Written as a one-time repair for rows produced by the pre-index race, but it carries **no guard making it one-time**.
5. `CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_generation … WHERE generation_id IS NOT NULL` — [db.py:187-191](../../orchestrator/app/db.py#L187-L191), then `commit()` — [db.py:192](../../orchestrator/app/db.py#L192).

**One message per generation, enforced in the database.** The rationale is documented at
[db.py:173-179](../../orchestrator/app/db.py#L173-L179): two browser tabs attached to the same detached
answer append at the same moment, so an application-level "select then insert" loses the race. The
partial unique index makes the second `INSERT` fail with `sqlite3.IntegrityError`, which `add_message`
([db.py:736-743](../../orchestrator/app/db.py#L736-L743)) converts into a no-op returning the winning
row with `"deduplicated": True` — and **re-raises when no `generation_id` was supplied**
([db.py:743](../../orchestrator/app/db.py#L743)).

**State & side effects** — Creates `app.sqlite3` plus its `-wal` and `-shm` sidecars. Writes on:
`create_user` [db.py:216](../../orchestrator/app/db.py#L216), `create_conversation`
[:271](../../orchestrator/app/db.py#L271), `update_conversation` [:324](../../orchestrator/app/db.py#L324),
`delete_conversation` [:336](../../orchestrator/app/db.py#L336), `truncate_messages`
[:603,609](../../orchestrator/app/db.py#L603), `replace_messages` [:642,657,671](../../orchestrator/app/db.py#L642),
`add_message` [:722,744](../../orchestrator/app/db.py#L722), `save_summary`
[:415](../../orchestrator/app/db.py#L415), `clear_summary` [:434,438](../../orchestrator/app/db.py#L434),
`add_conversation_chunks` [:451](../../orchestrator/app/db.py#L451), `save_upload`
[:500](../../orchestrator/app/db.py#L500), `save_url_document` [:760](../../orchestrator/app/db.py#L760),
`save_repo` [:795](../../orchestrator/app/db.py#L795), `replace_repo_chunks`
[:829,833](../../orchestrator/app/db.py#L829) — **plus the migration `DELETE` and `CREATE INDEX` on
every single `connect()`**. No network egress, no GPU, no module-level mutable state, no import-time
side effects.

**Dependencies** — Inbound: [auth.py](../../orchestrator/app/auth.py),
[history.py:27](../../orchestrator/app/history.py#L27) (14 route call sites),
[uploads.py](../../orchestrator/app/uploads.py), [health.py:76-81](../../orchestrator/app/health.py#L76-L81),
[recall.py:27](../../orchestrator/app/recall.py#L27),
[memory_recall.py:71](../../orchestrator/app/memory_recall.py#L71),
[main.py:16,162,339,469,487,527,758,770](../../orchestrator/app/main.py#L339),
[engines/repo.py:13,122](../../orchestrator/app/engines/repo.py#L13),
[engines/dataset.py:23](../../orchestrator/app/engines/dataset.py#L23),
[engines/url.py:15](../../orchestrator/app/engines/url.py#L15). Outbound: stdlib only, plus
[`app.config.settings`](../../orchestrator/app/config.py) at
[db.py:20](../../orchestrator/app/db.py#L20).

**Config** — `settings.app_db_path` ([db.py:197](../../orchestrator/app/db.py#L197)) is the only
setting this module reads. Default `/data/app.sqlite3`
([config.py:255](../../orchestrator/app/config.py#L255)); `APP_DB_PATH` is not forwarded by compose,
so the default is what runs.

**Failure modes**
- **`DATA-03` — the orphan set.** `messages`, `conversation_summaries` and `conversation_chunks`
  **do** declare `REFERENCES conversations(id) ON DELETE CASCADE`
  ([db.py:40](../../orchestrator/app/db.py#L40), [:56](../../orchestrator/app/db.py#L56),
  [:71](../../orchestrator/app/db.py#L71)) and `PRAGMA foreign_keys=ON`
  ([db.py:202](../../orchestrator/app/db.py#L202)) makes those cascades fire. But `uploads`
  ([:84-93](../../orchestrator/app/db.py#L84-L93)), `url_documents`
  ([:102-110](../../orchestrator/app/db.py#L102-L110)), `repos`
  ([:114-122](../../orchestrator/app/db.py#L114-L122)) and `repo_chunks`
  ([:123-131](../../orchestrator/app/db.py#L123-L131)) declare **no foreign key at all** — their
  `conversation_id` is a plain `TEXT NOT NULL`. `delete_conversation`
  ([db.py:334-340](../../orchestrator/app/db.py#L334-L340)) issues exactly one statement,
  `DELETE FROM conversations WHERE id = ? AND user_id = ?`, and there is no cleanup elsewhere.
  **Deleting a conversation therefore orphans its uploads, fetched URL text, cloned-repo records and
  every repo code chunk permanently** — including the full page text and source code held in
  `url_documents.text` and `repo_chunks.text`, which is the largest content in the database.
- **No busy-timeout tuning** ([db.py:199](../../orchestrator/app/db.py#L199)) → `sqlite3.OperationalError:
  database is locked` after 5 s of write contention, surfacing as an HTTP 500.
- **No bound** on `list_messages` ([db.py:343-354](../../orchestrator/app/db.py#L343-L354)),
  `get_conversation_chunks` ([:466](../../orchestrator/app/db.py#L466)), `get_url_documents`
  ([:769](../../orchestrator/app/db.py#L769)) or `get_uploads` ([:518](../../orchestrator/app/db.py#L518)) —
  whole-table reads per conversation with unbounded row count and unbounded `text`/`embedding` size.
- **Nothing is swallowed inside `db.py`** — there is no bare `except` in the file. Swallowing happens
  in callers ([main.py:340,472,494,528](../../orchestrator/app/main.py#L340)).
- `list_messages` ([db.py:343](../../orchestrator/app/db.py#L343)) is the only conversation accessor
  with **no ownership parameter**; correctness depends entirely on the caller checking first.

**Concurrency** — Every function is synchronous with a fresh short-lived connection per operation, so
`check_same_thread=True` is safe. Routes in [history.py](../../orchestrator/app/history.py) are plain
`def` and run in the anyio threadpool; callers in [main.py](../../orchestrator/app/main.py) are
`async def` and call these blocking functions **on the event loop**
([main.py:339,471,493,527,758,770](../../orchestrator/app/main.py#L339)) — each of which also re-runs
the full schema + migrate path. Cross-process races are handled in SQL: the partial unique index is the
documented race fix, and `truncate_messages` uses optimistic concurrency on `expected_total`
([db.py:599-600](../../orchestrator/app/db.py#L599-L600)). Remaining window: `replace_messages`
DELETE-then-INSERT is atomic ([db.py:642-670](../../orchestrator/app/db.py#L642-L670)) but the
read-modify-write in the *client* that produces the payload is not, so two tabs syncing concurrently
both pass the count check and the later write wins whole.

**Complexity hotspots** — `add_message` [db.py:678](../../orchestrator/app/db.py#L678) = **71 LOC**
(nested function inside a transaction context, plus a try/except-with-fallback-return);
`replace_messages` [db.py:616](../../orchestrator/app/db.py#L616) = 60 LOC; `search_repo_chunks`
[db.py:844](../../orchestrator/app/db.py#L844) = 51 LOC (three separately built SQL fragments plus a
hand-ordered parameter list whose correctness rests on a comment at
[db.py:875-876](../../orchestrator/app/db.py#L875-L876)); `migrate`
[db.py:153](../../orchestrator/app/db.py#L153) = 40 LOC. `db.py` at 1,064 LOC is the largest file in
the monorepo.

**Findings** — `DATA-03` (above), `PERF-03` (`connect()` re-runs the whole schema script plus
`migrate()` — including an unconditional full-scan `DELETE` and a `CREATE INDEX` — on every call site,
[db.py:195-205](../../orchestrator/app/db.py#L195-L205); measured here: **15** statements in `_SCHEMA`
(9 `CREATE TABLE` + 6 `CREATE INDEX`) and **34** call sites (31 `closing(connect())` inside `db.py`
plus 3 external), where the report's `PERF-03` text says 13 and 32), `SEC-02` (the ownership check
that guards these tables falls open on a DB exception, [main.py:338-344](../../orchestrator/app/main.py#L338-L344)),
`TEST-02`.

---

## DuckDB warehouse — `/data/warehouse.duckdb`

**Purpose** — The analytics data plane: one table per synced Salesforce object, plus one metadata
table holding the incremental-sync watermarks. It is the only thing the SQL engine may query.

**Public surface** — the schema itself:

| Object | DDL | `file:line` |
|---|---|---|
| `"<ObjectName>"` × 48 | `CREATE TABLE "<obj>" AS SELECT * FROM _staging_df` — **no PK, no index, no constraints, no types declared** | [storage.py:140](../../sync-worker/syncworker/storage.py#L140) |
| `_sync_meta` | `CREATE TABLE IF NOT EXISTS "_sync_meta" (object_name VARCHAR PRIMARY KEY, watermark VARCHAR, updated_at TIMESTAMP)` | [storage.py:80-85](../../sync-worker/syncworker/storage.py#L80-L85) |

Object tables are named from [`sync-worker/config.yaml`](../../sync-worker/config.yaml) (48 objects,
631 fields) after passing `_safe_ident`
([storage.py:27-30](../../sync-worker/syncworker/storage.py#L27-L30)); `config.py` validates the same
names first ([config.py:73-77](../../sync-worker/syncworker/config.py#L73-L77)), so no injection path
exists through configuration.

**Every column is `VARCHAR`.** `normalize_records`
([storage.py:38-56](../../sync-worker/syncworker/storage.py#L38-L56)) casts every value to `str` or
`None` — deliberately, to keep column types stable across Bulk CSV (strings) and REST JSON (typed)
batches — and `CREATE TABLE AS SELECT` then materialises them all as `VARCHAR`. Measured consequence
on this repo's own DuckDB: `SUM(Amount)` → `BinderException: No function matches … 'sum(VARCHAR)'`;
`WHERE Amount > 1000` → `BinderException`; `date_trunc('month', CloseDate)` → `BinderException`;
`AVG(TRY_CAST(Amount AS DOUBLE))` works. The SQL-writing model sees the declared types verbatim,
because [schema_cache.py:50-55](../../orchestrator/app/core/schema_cache.py#L50-L55) reads
`information_schema.columns … WHERE table_schema = 'main'` and `format_schema`
([:65-71](../../orchestrator/app/core/schema_cache.py#L65-L71)) renders
`Opportunity(Id VARCHAR, Amount VARCHAR, …)` straight into the prompt.

**Control flow** — one batch:
1. `Store.__init__` opens `duckdb.connect(db_path)` read-write and creates `_sync_meta` if absent — [storage.py:75-85](../../sync-worker/syncworker/storage.py#L75-L85).
2. `upsert` registers the pandas frame as `_staging_df` — [storage.py:136](../../sync-worker/syncworker/storage.py#L136).
3. **First batch for an object**: `BEGIN` → `CREATE TABLE … AS SELECT *` → `COMMIT` — [storage.py:138-142](../../sync-worker/syncworker/storage.py#L138-L142).
4. **Subsequent batches**: diff `DESCRIBE "<obj>"` against `DESCRIBE SELECT * FROM _staging_df` to find drifted columns — [storage.py:145-147](../../sync-worker/syncworker/storage.py#L145-L147).
5. `BEGIN` → **`ALTER TABLE "<obj>" ADD COLUMN "<new>" <type>`** per new column ([storage.py:151-154](../../sync-worker/syncworker/storage.py#L151-L154)) → `DELETE … WHERE Id IN (SELECT Id FROM _staging_df)` ([:155-157](../../sync-worker/syncworker/storage.py#L155-L157)) → `INSERT … BY NAME SELECT *` ([:158-160](../../sync-worker/syncworker/storage.py#L158-L160)) → `COMMIT`; `ROLLBACK` + re-raise on any exception ([:162-164](../../sync-worker/syncworker/storage.py#L162-L164)).
6. After **all** batches for the object, `set_watermark` writes `cycle_start` — [main.py:188](../../sync-worker/syncworker/main.py#L188). See the ordering guarantee in [sync-worker.md](sync-worker.md).

**Schema drift is additive only** ([storage.py:151-154](../../sync-worker/syncworker/storage.py#L151-L154)):
a field newly adopted at runtime ([main.py:142-145](../../sync-worker/syncworker/main.py#L142-L145)) gets a
column, but **existing rows are left `NULL` and nothing backfills them**, because a full extract only
happens when the watermark is `NULL` ([main.py:147](../../sync-worker/syncworker/main.py#L147)). The
resulting data shape is asserted by
[`tests/test_upsert.py:86-94`](../../sync-worker/tests/test_upsert.py#L86-L94). A column removed from
the config is never dropped.

**State & side effects** — All writes come from the sync worker. Every orchestrator handle is opened
**read-only and externally sandboxed**:

| Consumer | Open call | Config |
|---|---|---|
| SQL engine | [sql.py:124-132](../../orchestrator/app/engines/sql.py#L124-L132) | `read_only=True`, `enable_external_access=False`, no auto-install/auto-load of extensions |
| Schema cache | [schema_cache.py:40-48](../../orchestrator/app/core/schema_cache.py#L40-L48) | identical config — DuckDB rejects concurrent connections to one file whose configs differ |
| Health probe | [health.py:54-56](../../orchestrator/app/health.py#L54-L56) | `read_only=True` |

`enable_external_access=False` is what makes `read_csv`, `read_blob`, `glob` and `httpfs` raise
`PermissionException`, and is the reason `SEC-07` (the `sql_guard` E-string bypass) is not currently
exploitable.

**Dependencies** — Inbound writer: [storage.py](../../sync-worker/syncworker/storage.py) via
[main.py:167](../../sync-worker/syncworker/main.py#L167). Inbound readers: the three above.
Outbound: `duckdb`, `pandas`, `pyarrow`.

**Config** — `DUCKDB_PATH`, set identically for both services at
[docker-compose.yml:245](../../docker-compose.yml#L245) and [:316](../../docker-compose.yml#L316),
read at [config.py:43](../../sync-worker/syncworker/config.py#L43) and
[config.py:96](../../orchestrator/app/config.py#L96).

**Failure modes**
- **`DATA-02`** — because the table is created by `CREATE TABLE AS SELECT`
  ([storage.py:140](../../sync-worker/syncworker/storage.py#L140)) there is **no PRIMARY KEY and no
  index on `Id`**. The per-batch `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM _staging_df)`
  ([storage.py:156](../../sync-worker/syncworker/storage.py#L156)) therefore full-scans the entire
  object table once per 10,000-row batch, for all 48 objects, every 30 minutes.
- `duckdb.connect` ([storage.py:79](../../sync-worker/syncworker/storage.py#L79)) raises
  `duckdb.IOException` if another process holds the write lock; that bubbles to
  [main.py:292](../../sync-worker/syncworker/main.py#L292) and triggers exponential backoff.
- **`_sync_meta` lives in the `main` schema**, so
  [schema_cache.py:47-55](../../orchestrator/app/core/schema_cache.py#L47-L55) exposes it to the
  SQL-writing LLM as if it were a business table.
- **Salesforce deletes are never propagated** ([main.py:147-154](../../sync-worker/syncworker/main.py#L147-L154)):
  the incremental query filters on `SystemModstamp >` only, so a deleted record stays in DuckDB
  forever. There is no soft-delete column and no reconciliation pass.
- `_sync_meta` rows for objects removed from the config are never deleted
  ([storage.py:99-106](../../sync-worker/syncworker/storage.py#L99-L106) only inserts/updates), so a
  removed-then-re-added object silently resumes incrementally instead of doing a full extract.
- **No retention, no compaction, no VACUUM** anywhere.

**Concurrency** — DuckDB permits exactly one writer per file. The sync worker opens a fresh `Store`
per cycle and closes it ([main.py:285,289](../../sync-worker/syncworker/main.py#L285)), so the write
window is bounded to a cycle. The split is safe **only by convention** — compose grants both
containers read-write access to the volume ([:269,320](../../docker-compose.yml#L269)) and nothing
enforces the orchestrator's `read_only=True`.

**Complexity hotspots** — `Store.upsert` = 44 LOC
([storage.py:124-167](../../sync-worker/syncworker/storage.py#L124-L167)), cyclomatic ≈ 8. Under both
thresholds.

**Findings** — `DATA-02`, `SEC-07` (guard bypass, neutralised by the read-only + no-external-access
handle at [sql.py:124-132](../../orchestrator/app/engines/sql.py#L124-L132)), `PERF-01` (the
blocking `_execute` at [sql.py:201,206](../../orchestrator/app/engines/sql.py#L201) runs inline in an
`async def`), `DATA-01` (the SQL retry path skips the known-table hallucination guard,
[sql.py:203-207](../../orchestrator/app/engines/sql.py#L203-L207)).

---

## LanceDB — `/data/lancedb`, table `chunks`

**Purpose** — The semantic-search index over Salesforce long-text fields: one row per text chunk,
carrying its embedding and enough provenance to trace back to a record and field.

**Public surface** — the schema, created at
[rag_index.py:79-89](../../sync-worker/syncworker/rag_index.py#L79-L89):

| Field | Arrow type | Meaning |
|---|---|---|
| `vector` | `pa.list_(pa.float32(), dim)` — **fixed-size list, dimension bound at creation** | the embedding |
| `text` | `pa.string()` | the chunk text |
| `object` | `pa.string()` | Salesforce object name |
| `record_id` | `pa.string()` | Salesforce 15/18-char Id |
| `field` | `pa.string()` | which `rag_field` the chunk came from |
| `system_modstamp` | `pa.string()` | the record's `SystemModstamp` at index time |

Table name `chunks` is the constant `TABLE_NAME`
([rag_index.py:24](../../sync-worker/syncworker/rag_index.py#L24)) on the write side and
`settings.lancedb_table` (default `"chunks"`,
[config.py:98](../../orchestrator/app/config.py#L98)) on the read side. `LANCEDB_TABLE` is not
forwarded by compose, so the two agree by default rather than by construction.

**Control flow** — how the dimension is discovered, and the re-index policy:
1. `index_records` builds row dicts with **no `vector` key** and no comparison against what is already stored — [rag_index.py:104-125](../../sync-worker/syncworker/rag_index.py#L104-L125).
2. One `embed()` call covers the whole batch — [rag_index.py:128](../../sync-worker/syncworker/rag_index.py#L128); vectors are attached at [:129-130](../../sync-worker/syncworker/rag_index.py#L129-L130).
3. **`_open_or_create_table(dim=len(rows[0]["vector"]))`** — [rag_index.py:131](../../sync-worker/syncworker/rag_index.py#L131). The dimension is therefore **discovered at runtime, from the length of the first embedding the model returns** — never configured, never asserted against `EMBED_MODEL`.
4. Inside `_open_or_create_table` ([:73-89](../../sync-worker/syncworker/rag_index.py#L73-L89)): if `chunks` already exists, `db.open_table` is returned and **`dim` is ignored entirely** ([:77-78](../../sync-worker/syncworker/rag_index.py#L77-L78)); otherwise the schema above is created with that dimension baked in.
5. If the batch produced no rows, `_open_table_if_exists()` is used instead so that cleared long-text fields still get their stale chunks removed — [:132-137](../../sync-worker/syncworker/rag_index.py#L132-L137).
6. **Delete-then-reinsert**: `for rid in record_ids: table.delete(f"record_id = '{rid}'")` — [rag_index.py:141-142](../../sync-worker/syncworker/rag_index.py#L141-L142) — **one LanceDB delete per record** — followed by a single `table.add(rows)` — [:144](../../sync-worker/syncworker/rag_index.py#L144). Every chunk of a changed record is replaced wholesale; there is no per-chunk diff.

Read path: [rag.py:41-49](../../orchestrator/app/engines/rag.py#L41-L49) — embed the query through
`EMBED_BASE_URL`, `lancedb.connect(settings.lancedb_dir)`, `db.open_table(settings.lancedb_table)`,
`table.search(vector).limit(rag_top_k).to_list()`.

**State & side effects** — `lancedb.connect` ([rag_index.py:70](../../sync-worker/syncworker/rag_index.py#L70))
creates the directory; `create_table` ([:89](../../sync-worker/syncworker/rag_index.py#L89)),
`delete` ([:142](../../sync-worker/syncworker/rag_index.py#L142)) and `add`
([:144](../../sync-worker/syncworker/rag_index.py#L144)) write to it. Each index pass is preceded by a
**GPU call**: `POST {EMBED_VIA}/embeddings` ([:47-50](../../sync-worker/syncworker/rag_index.py#L47-L50))
against `Qwen/Qwen3-Embedding-0.6B` ([docker-compose.yml:191](../../docker-compose.yml#L191)).

**Dependencies** — Inbound writer: [main.py:176](../../sync-worker/syncworker/main.py#L176). Inbound
reader: [rag.py:41-49](../../orchestrator/app/engines/rag.py#L41-L49). Outbound: `lancedb` and
`pyarrow`, both imported lazily ([:68,74](../../sync-worker/syncworker/rag_index.py#L68)).

**Config** — `LANCEDB_DIR` ([docker-compose.yml:246,317](../../docker-compose.yml#L246)); `EMBED_VIA`
/ `EMBED_MODEL` on the write side ([:314-315](../../docker-compose.yml#L314-L315)),
`EMBED_BASE_URL` / `EMBED_MODEL` on the read side ([:241-242](../../docker-compose.yml#L241-L242)) —
two different variable names for the same endpoint. `LANCEDB_TABLE` and `RAG_TOP_K` are read by
[config.py:98](../../orchestrator/app/config.py#L98) but never forwarded.

**Failure modes**
- **The dimension is a one-way latch.** Changing `EMBED_MODEL` to a different-dimension model leaves
  the existing `chunks` table in place ([:77-78](../../sync-worker/syncworker/rag_index.py#L77-L78));
  `table.add` then fails on a schema mismatch, and
  [main.py:177-184](../../sync-worker/syncworker/main.py#L177-L184) swallows that failure on every
  subsequent cycle while the watermark keeps advancing
  ([main.py:188](../../sync-worker/syncworker/main.py#L188)). The index silently stops growing.
- **No re-embedding guard**: no content hash, no `system_modstamp` comparison, no chunk-level dedup.
  Any change to any field of a record re-embeds all of its long-text chunks on the GPU.
- **No vector index is ever created** — [:79-89](../../sync-worker/syncworker/rag_index.py#L79-L89)
  defines a schema only, and no `create_index` call exists in the repo — so
  `table.search(...)` ([rag.py:46](../../orchestrator/app/engines/rag.py#L46)) is a brute-force scan of
  every chunk.
- **Deleted Salesforce records keep their chunks forever** — deletion only happens for `record_id`s
  present in a change batch ([:141-142](../../sync-worker/syncworker/rag_index.py#L141-L142)).
- **Filter injection is guarded but fragile**: `record_id` is validated against
  `^[a-zA-Z0-9]{15,18}$` ([:26,108](../../sync-worker/syncworker/rag_index.py#L108)) before being
  interpolated into the delete predicate at [:142](../../sync-worker/syncworker/rag_index.py#L142) —
  the only thing between config-driven data and a string-built filter.
- Chunk quality is bounded by the whitespace tokenizer
  ([chunking.py:30](../../sync-worker/syncworker/chunking.py#L30)); the 12 JSON `rag_fields` in
  [`config.yaml`](../../sync-worker/config.yaml) can produce a single chunk far larger than the
  embedder's 4,096-token window ([docker-compose.yml:194](../../docker-compose.yml#L194)).

**Concurrency** — Written only by the single-threaded sync worker, inline in the batch loop
([main.py:174-176](../../sync-worker/syncworker/main.py#L174-L176)), so GPU embedding blocks
Salesforce pagination. The orchestrator opens its own connection per `retrieve()` call
([rag.py:43-44](../../orchestrator/app/engines/rag.py#L43-L44)) — concurrent reads during a write pass
are not coordinated by anything in this repo.

**Complexity hotspots** — `index_records` = 58 LOC
([rag_index.py:97-154](../../sync-worker/syncworker/rag_index.py#L97-L154)), cyclomatic ≈ 12 — over the
complexity threshold.

**Findings** — `TEST-02` (`RagIndexer` has **zero** test coverage; only `OpenAIEmbedder` is tested),
`SEC-05` (chunk text originating from Salesforce long-text fields reaches the model prompt with no
provenance tainting).

---

## Retention

| Store | Policy | Enforced where |
|---|---|---|
| `/data/workspaces/**` (cloned repos, extracted uploads) | **`WORKSPACE_TTL_HOURS = 24`** ([config.py:215](../../orchestrator/app/config.py#L215), [docker-compose.yml:266](../../docker-compose.yml#L266)) then **`WORKSPACE_QUOTA_GB = 20`** ([config.py:216](../../orchestrator/app/config.py#L216), [:267](../../docker-compose.yml#L267)) | [repo.py:94-119](../../orchestrator/app/core/repo.py#L94-L119) |
| Everything else | **none** | — |

`enforce_quota_and_ttl` ([repo.py:94-119](../../orchestrator/app/core/repo.py#L94-L119)) walks the
top-level directories under `WORKSPACE_DIR`, `shutil.rmtree`s any whose mtime is older than
`workspace_ttl_hours * 3600` ([:109-110](../../orchestrator/app/core/repo.py#L109-L110)), then — if the
survivors still exceed `workspace_quota_gb * 1024**3` — deletes **oldest first** until under quota
([:113-119](../../orchestrator/app/core/repo.py#L113-L119)).

It is **not a timer**. It has exactly two call sites, both lazy:
[uploads.py:86-88](../../orchestrator/app/uploads.py#L86-L88) (before writing a new upload) and
[engines/repo.py:32](../../orchestrator/app/engines/repo.py#L32) (before a clone). On an idle
deployment nothing is ever swept, and both `WORKSPACE_TTL_HOURS` and `WORKSPACE_QUOTA_GB` **are**
forwarded by compose while `WORKSPACE_DIR` is not, so the sweep always targets
`/data/workspaces` ([config.py:214](../../orchestrator/app/config.py#L214)).

The design deliberately survives the sweep: an upload's **profile** is stored in SQLite, not the
bytes, "so a conversation keeps answering questions after the workspace TTL has swept the extracted
files away" ([db.py:81-83](../../orchestrator/app/db.py#L81-L83)), and
`bytes_available` ([uploads.py:38-41](../../orchestrator/app/uploads.py#L38-L41)) is the runtime probe
for whether the files are still there.

**What has no retention policy at all:**

| Store | Growth driver | Evidence |
|---|---|---|
| `/data/parquet/<object>/*.parquet` | one file per batch, per object, per cycle — 48 objects × 48 cycles/day; **nothing in the repo ever reads these files back** | [storage.py:59-69](../../sync-worker/syncworker/storage.py#L59-L69) |
| DuckDB object tables | rows only ever added or replaced; Salesforce deletes never propagate; no VACUUM | [storage.py:140,155-160](../../sync-worker/syncworker/storage.py#L140) |
| DuckDB `_sync_meta` | rows for de-configured objects are never removed | [storage.py:99-106](../../sync-worker/syncworker/storage.py#L99-L106) |
| LanceDB `chunks` | deleted only for record ids present in a change batch | [rag_index.py:141-142](../../sync-worker/syncworker/rag_index.py#L141-L142) |
| `/reports/*` (DOCX/PDF/XLSX exports) | written per report/export request; `list_reports` only enumerates, never prunes | [report.py:230-256](../../orchestrator/app/engines/report.py#L230-L256), [report_paths.py:51-70](../../orchestrator/app/core/report_paths.py#L51-L70) |
| `messages`, `conversation_chunks`, `conversation_summaries` | rows disappear only via the FK cascade when a conversation is deleted | [db.py:40,56,71](../../orchestrator/app/db.py#L40) |
| `uploads`, `url_documents`, `repos`, `repo_chunks` | **never disappear** — no FK, no cascade, no TTL, no delete path | [db.py:84,102,114,123](../../orchestrator/app/db.py#L84); [db.py:334-340](../../orchestrator/app/db.py#L334-L340) |
| `hf-cache` volume | model weights, shared by five services | [docker-compose.yml:82,131,168,200,271](../../docker-compose.yml#L82) |

No service declares a disk quota, and there is no healthcheck or alert on free space anywhere in
[`docker-compose.yml`](../../docker-compose.yml).

**Data-model findings**: `DATA-02`, `DATA-03`, `PERF-03`, `DATA-01`, `SEC-07`, `TEST-02`.
</content>
