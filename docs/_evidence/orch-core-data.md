# Evidence — `orch-core-data`

Scope: `orchestrator/app/core/{salesforce,schema_cache,sf_dictionary,repo_index,repo,extract,profile}.py`.
All seven files read in full with the Read tool. Every line reference below was read or re-verified with `rg -n`.
Total assigned LOC: **1249** (`wc -l`: salesforce 268, schema_cache 74, sf_dictionary 192, repo_index 54, repo 302, extract 108, profile 251).

No `TODO` / `FIXME` / `HACK` / `XXX` markers exist in any assigned file (`rg -n 'TODO|FIXME|HACK|XXX'` → exit 1, no matches).

No secret values were read or copied. `.env` (mode `0600`) and `secrets/` (mode `0700`) were listed only; variable *names* appear below, values never.

---

### orchestrator/app/core/salesforce.py  (268 LOC)

**Purpose** — Live read-only Salesforce REST access (OAuth client-credentials → SOQL `/query` and describe APIs) used when the synced DuckDB warehouse is stale or lacks the object. Also guards model-generated SOQL and merges live rows over warehouse rows.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `MAX_ROWS` | `= 200` | `orchestrator/app/core/salesforce.py:29` |
| `_FORBIDDEN` | `re.compile(r"\b(INSERT\|UPDATE\|DELETE\|UPSERT\|MERGE\|UNDELETE\|CREATE\|ALTER\|DROP\|GRANT\|REVOKE)\b", re.I)` | `orchestrator/app/core/salesforce.py:33-36` |
| `SalesforceUnavailable(RuntimeError)` | class | `orchestrator/app/core/salesforce.py:39` |
| `UnsafeSoql(ValueError)` | class | `orchestrator/app/core/salesforce.py:43` |
| `configured()` | `() -> bool` | `orchestrator/app/core/salesforce.py:47` |
| `guard_soql(soql)` | `(str) -> str` — raises `UnsafeSoql` | `orchestrator/app/core/salesforce.py:55` |
| `_Token` | class, `TTL = 25 * 60` | `orchestrator/app/core/salesforce.py:93`, TTL at `:97` |
| `_token` | module-global `_Token()` instance | `orchestrator/app/core/salesforce.py:108` |
| `_authenticate()` | `async () -> Tuple[str, str]` | `orchestrator/app/core/salesforce.py:111` |
| `run_soql(soql)` | `async (str) -> Tuple[str, List[Dict]]` | `orchestrator/app/core/salesforce.py:144` |
| `_get(path, params=None)` | `async (str, Optional[dict]) -> Any` | `orchestrator/app/core/salesforce.py:177` |
| `list_objects()` | `async () -> List[Dict[str, Any]]` | `orchestrator/app/core/salesforce.py:191` |
| `describe_object(name)` | `async (str) -> Dict[str, Any]` | `orchestrator/app/core/salesforce.py:211` |
| `_clean(record)` | `(Dict) -> Dict` — drops `attributes` | `orchestrator/app/core/salesforce.py:229` |
| `merge_rows(local, live)` | `(List[Dict], List[Dict]) -> List[Dict]` | `orchestrator/app/core/salesforce.py:234` |

**Control flow** (primary path `run_soql`)
1. `run_soql` calls `guard_soql(soql)` — `orchestrator/app/core/salesforce.py:146`.
2. `guard_soql` rejects empty (`:60-61`), whitespace-normalises and `rstrip(";")` (`:62-63`), rejects any remaining `;` (`:65-66`), requires a leading `SELECT` (`:67-68`), scans `_FORBIDDEN` (`:69-71`), requires a `FROM` (`:72-73`).
3. `SELECT COUNT()` special case: any trailing `LIMIT n` is *stripped* and the query returned uncapped — `orchestrator/app/core/salesforce.py:79-80`.
4. Otherwise a trailing `LIMIT n` is lowered to `MAX_ROWS` if larger (`:84-87`), or ` LIMIT 200` is appended if absent (`:88-89`).
5. `_authenticate()` — `orchestrator/app/core/salesforce.py:147`. Returns the cached token if `not _token.stale()` (`:114-115`); otherwise requires `settings.sf_client_secret` (`:117-122`) and POSTs `grant_type=client_credentials` to `{sf_login_url}/services/oauth2/token` with `httpx.AsyncClient(timeout=30.0)` — `:123-131`.
6. Non-200 → `SalesforceUnavailable(f"...HTTP {resp.status_code}")`, body deliberately not echoed — `:132-136`.
7. `body["access_token"]` / `body["instance_url"]` are read with bare subscripting (no `.get`) and cached with `time.monotonic()` — `:137-141`.
8. GET `{instance}/services/data/{sf_api_version}/query?q=<safe>` with `Authorization: Bearer` and `timeout=settings.sf_live_timeout` — `:148-153`.
9. On HTTP 401 the cached token is cleared and the whole GET is retried exactly once — `:154-162`.
10. Any other non-200: the response JSON's `[0]["message"][:200]` is spliced into the exception text — `:163-171`.
11. Rows: `resp.json().get("records", [])`, `_clean` applied to *all* of them, then sliced to `MAX_ROWS` — `:173-174`.

Secondary path `describe_object`: name validated against `^[A-Za-z][A-Za-z0-9_]*$` (`:213-214`) then `_get` (`:215-217`) → `_authenticate` (`:179`) → GET (`:180-185`) → non-200 raises with only the status code (`:186-187`).

**State & side effects**
- Network egress: `POST {settings.sf_login_url}/services/oauth2/token` (`:124-131`); `GET {instance_url}/services/data/{version}/query` (`:149-153`, `:158-162`); `GET {instance_url}<path>` for describe/sobjects (`:181-185`). `instance_url` is whatever the token endpoint returns (`:139`) — the egress host is server-supplied, not pinned.
- Global mutation: `_token.value` / `_token.instance` / `_token.at` written at `:138-140` and `_token.value = None` at `:155`.
- No DB writes, no filesystem writes, no GPU/model calls.
- Env reads: indirect only, via `settings` (see Config).

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/live_sf.py:21` (`from ..core import salesforce`), `:113` (`from ..core import salesforce as sf`), `:145` (`salesforce.run_soql`), `:121` (`sf.describe_object`), `:129` (`sf.list_objects`); `orchestrator/app/engines/agent.py:289-290` and `:315` (`SalesforceUnavailable, UnsafeSoql, merge_rows`), `:304`/`:329` (`merge_rows`); `orchestrator/app/engines/sql.py:304` (`from ..core import salesforce as sf_live`), `:306` (`sf_live.configured()`); `orchestrator/tests/test_live_salesforce.py:10`, `:212`.
- Outbound: `re`, `time`, `typing`, `httpx` (`:20-24`), `..config.settings` (`:26`).

**Config** (all via `settings`, defined in `orchestrator/app/config.py`)
- `settings.sf_client_id` — `salesforce.py:50`, `:128`; env `SF_CLIENT_ID` at `orchestrator/app/config.py:118`.
- `settings.sf_client_secret` — `salesforce.py:51`, `:117`, `:129`; env `SF_CLIENT_SECRET` at `orchestrator/app/config.py:119`.
- `settings.sf_login_url` — `salesforce.py:50`, `:125`; env `SF_LOGIN_URL` at `orchestrator/app/config.py:120`.
- `settings.sf_private_key_b64` — `salesforce.py:51`; env `SF_PRIVATE_KEY_B64` at `orchestrator/app/config.py:121`.
- `settings.sf_api_version` — `salesforce.py:150`, `:159`, `:199`, `:216`; env `SF_API_VERSION`, default `"v61.0"`, at `orchestrator/app/config.py:122`.
- `settings.sf_live_timeout` — `salesforce.py:148`, `:157`, `:180`; env `SF_LIVE_TIMEOUT`, default `"45"`, at `orchestrator/app/config.py:123`.
- Gating flag `settings.sf_live_enabled` (`orchestrator/app/config.py:124`) is *not* consulted inside this module — only by the caller at `orchestrator/app/engines/sql.py:306`.
- `.env.example` documents `SF_CLIENT_ID` / `SF_USERNAME` / `SF_LOGIN_URL` / `SF_PRIVATE_KEY_B64` (`.env.example:18-21`) but **not** `SF_CLIENT_SECRET`, `SF_API_VERSION` or `SF_LIVE_TIMEOUT`, which are the only vars this module can actually authenticate with (`salesforce.py:117-122`).

**Failure modes**
- Raises: `UnsafeSoql` (`:61,66,68,71,73,214`), `SalesforceUnavailable` (`:113,120,134,171,187`).
- **Unwrapped exceptions escape**: `httpx.ConnectError`, `httpx.ConnectTimeout`, `httpx.ReadTimeout`, `httpx.RemoteProtocolError` from `:124`, `:150`, `:159`, `:181` are not caught anywhere in the module. `json.JSONDecodeError` from `resp.json()` at `:137`, `:173`, `:188`. `KeyError` from `body["access_token"]` / `body["instance_url"]` at `:138-139` if the token endpoint returns a 200 with a different shape.
- Swallowed: bare `except Exception: pass` around the error-detail parse at `:169-170` (benign, detail only).
- No retry/backoff on `_authenticate` (`:111-141`) for 5xx or network failure — a single transient blip raises.
- `_get` (`:177-188`) has **no 401 retry**, unlike `run_soql` (`:154-162`). An expired cached token makes `list_objects` / `describe_object` fail hard.
- No response-body size bound anywhere: `resp.json()` at `:173` will materialise an arbitrarily large Salesforce response before any capping happens.
- No circuit breaker; every failed request costs the full `sf_live_timeout` (default 45 s).

**Concurrency**
- All network functions are `async def`; all use `httpx.AsyncClient` correctly (no blocking calls inside async).
- **A new `httpx.AsyncClient` is constructed per call** (`:123`, `:148`, `:157`, `:180`) — no connection pooling or keep-alive across requests; TLS handshake per query.
- `_token` (`:108`) is shared module-level mutable state with **no `asyncio.Lock`**. Race windows: (a) N concurrent questions all see `_token.stale()` true and all POST the token endpoint (thundering herd, `:114`→`:124`); (b) request A sets `_token.value = None` at `:155` while request B is mid-`_get`, causing B to re-authenticate unnecessarily; (c) two writers interleave `:138-140` so `value` and `instance` can transiently come from different token responses.
- `_Token.TTL = 25 * 60` (`:97`) is a fixed client-side guess; the server-side session timeout is org-configurable and can be shorter.

**Complexity hotspots** — none over 60 LOC. Largest: `merge_rows` `:234-268` (35 LOC), `guard_soql` `:55-90` (36 LOC), `run_soql` `:144-174` (31 LOC). Branch count in `guard_soql` is 8.

**Notable**
- Magic numbers: `MAX_ROWS = 200` (`:29`), `TTL = 25 * 60` (`:97`), hardcoded auth `timeout=30.0` (`:123`, not `sf_live_timeout`), `[:200]` message truncation (`:168`).
- `configured()` (`:47-52`) returns `True` when only `sf_private_key_b64` is set, but `_authenticate` then refuses at `:117-122` — the JWT grant is deliberately not implemented here (comment `:118-119`), so `configured()` over-reports.
- The `;` check at `:65` operates on the whole query including string literals, so a legitimate `WHERE Name = 'a;b'` is rejected as multi-statement. Likewise `_FORBIDDEN` (`:33-36`) matches inside literals: `WHERE Stage__c = 'Merge'` is refused.
- The `SELECT COUNT()` branch (`:79-80`) is the one path that returns a query with **no row cap at all**; the justification (a `COUNT()` returns one number) is correct for `COUNT()` but the regex is anchored to `^SELECT\s+COUNT\(\s*\)` so `SELECT COUNT(Id)` correctly falls through to the capped path (asserted by `orchestrator/tests/test_live_salesforce.py:271`).
- `merge_rows` docstring (`:237-247`) is longer than its body; only ever called as `merge_rows([], live_rows)` at `orchestrator/app/engines/agent.py:304` and `:329` — the warehouse-merge half of the function is currently dead in production.
- Duplication in a caller: `orchestrator/app/engines/agent.py:288-309` and `:311-334` are two byte-near-identical `if step.kind == "salesforce" and salesforce:` blocks; the second is unreachable.

---

### orchestrator/app/core/schema_cache.py  (74 LOC)

**Purpose** — TTL cache of `{table: [(column, dtype), ...]}` read from a read-only DuckDB connection, used to ground the text-to-SQL prompt.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `SchemaCache` | class | `orchestrator/app/core/schema_cache.py:11` |
| `SchemaCache.__init__` | `(self, ttl_seconds: float = 300.0)` | `:14` |
| `SchemaCache.get` | `(self, db_path: str, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]` | `:18` |
| `SchemaCache.invalidate` | `(self, db_path: str \| None = None) -> None` | `:27` |
| `SchemaCache._load` | `@staticmethod (db_path: str) -> Dict[...]` | `:33-34` |
| `format_schema` | `(schema) -> str` | `:65` |
| `schema_cache` | module-level `SchemaCache()` singleton, default TTL | `:74` |

**Control flow**
1. `get(db_path)` reads `time.monotonic()` — `:19`.
2. Cache hit returned when present, not force-refreshed, and inside the TTL — `:20-22`.
3. Miss → `_load(db_path)` — `:23`.
4. `_load` lazily `import duckdb` (`:35`) and opens `duckdb.connect(db_path, read_only=True, config={enable_external_access: False, autoinstall_known_extensions: False, autoload_known_extensions: False})` — `:40-48`.
5. Queries `information_schema.columns WHERE table_schema = 'main' ORDER BY table_name, ordinal_position` — `:50-55`; connection closed in `finally` — `:56-57`.
6. Rows folded into `{table: [(col, dtype)]}` — `:59-62`; result stored with the timestamp — `:24`.

**State & side effects**
- Filesystem read: opens the DuckDB file at `db_path` (`:40`); read-only, so no writes.
- Global mutation: `self._cache` dict on the module singleton `schema_cache` (`:74`, mutated at `:24`, `:29`, `:31`).
- No network egress, no GPU calls, no env reads.

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/sql.py:23` (import), `:189` `format_schema(schema_cache.get(settings.duckdb_path))`, `:191` `schema_cache.get(settings.duckdb_path)`; `orchestrator/app/engines/live_sf.py:22` (import), `:56` `schema_cache().keys()`; `orchestrator/tests/test_imports.py:13`; `orchestrator/tests/test_live_salesforce.py:257` monkeypatches `sql_engine.schema_cache.get`.
- Outbound: `time`, `typing`, lazily `duckdb` (`:35`).

**Config**
- Consumes **no** env vars directly. `db_path` is passed in — always `settings.duckdb_path` (`orchestrator/app/config.py:96`, env `DUCKDB_PATH`, default `/data/warehouse.duckdb`).
- `orchestrator/app/config.py:265` defines `self.schema_cache_ttl: float = _float("SCHEMA_CACHE_TTL", 300.0)`. `rg -n 'schema_cache_ttl|SCHEMA_CACHE_TTL'` across the whole repo returns **only that one line** — the setting is never read. `schema_cache = SchemaCache()` at `:74` takes the hardcoded 300.0 default.

**Failure modes**
- `duckdb.connect` at `:40` raises (`duckdb.IOException`) if the warehouse file is missing, locked by a writer with a different config, or corrupt. **Nothing is caught in this module** — the exception propagates to `orchestrator/app/engines/sql.py:189`.
- `import duckdb` at `:35` raises `ImportError` if the wheel is absent; not caught.
- No timeout on the DuckDB query (`:50-55`), no retry, no bound on the number of returned columns/tables — `format_schema` (`:65-71`) concatenates every column of every table into one prompt string with no character budget.
- A cache *miss* does not negative-cache: a persistently failing `_load` re-runs on every request.

**Concurrency**
- Entirely synchronous. Called from `async def` request handlers (`orchestrator/app/engines/sql.py:189` sits inside the async SQL engine), so the DuckDB connect + `information_schema` scan **blocks the event loop**.
- `self._cache` is mutated without a lock (`:24`). Two concurrent misses both run `_load` (duplicate DuckDB connections) and both write; last write wins. Benign for correctness, wasteful.
- `get()` returns the cached dict **by reference** (`:22`, `:25`) — a caller that mutates the returned dict corrupts the cache for every later request. No copy is made.

**Complexity hotspots** — none. Longest function `_load` `:33-62` (30 LOC).

**Notable**
- `invalidate()` (`:27-31`) is **dead code**: `rg -n 'invalidate' orchestrator/` matches only its own definition. Nothing invalidates the cache after a sync-worker run, so the model can be prompted with a schema up to 300 s stale, and a table added by the sync worker is invisible for up to 5 minutes.
- Caller bug found while verifying inbound edges: `orchestrator/app/engines/live_sf.py:56` calls `schema_cache()` — invoking the `SchemaCache` *instance*. `SchemaCache` defines no `__call__` (`:11-62`), so this always raises `TypeError`, which is swallowed by the `except Exception: return ""` at `orchestrator/app/engines/live_sf.py:57-58`. `_object_hint()` therefore always returns `""` and the SOQL prompt never gets the "objects known to be in this org" context it was written to carry.
- The DuckDB lockdown config at `:42-47` is duplicated from the SQL engine's `_execute` (comment at `:37-39` says so explicitly).
- `format_schema` (`:65-71`) does not sort tables; output order follows `information_schema` ordering, which is stable but undocumented.

---

### orchestrator/app/core/sf_dictionary.py  (192 LOC)

**Purpose** — Maps user vocabulary ("interview status") to Salesforce API names (`Interview_Status__c`) from an org export, and injects a compact per-question hint into SQL/SOQL prompts.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `DICTIONARY_PATH` | `os.environ.get("SF_DICTIONARY_PATH", "/data/sf_dictionary.json")` | `orchestrator/app/core/sf_dictionary.py:26-28` |
| `MAX_OBJECTS` | `= 4` | `:32` |
| `MAX_FIELDS_PER_OBJECT` | `= 60` | `:33` |
| `_WORD_RE` | `re.compile(r"[A-Za-z][A-Za-z0-9_]+")` | `:35` |
| `_STOP` | 30-word stop set | `:37-42` |
| `_cache` | module-global `Optional[Dict[str, Any]]` | `:44` |
| `_tokens(text)` | `(str) -> set` | `:47` |
| `build_from_rows(rows)` | `(Iterable[tuple]) -> Dict[str, Any]` | `:51` |
| `save(data, path=DICTIONARY_PATH)` | `-> None` | `:73` |
| `load(path=DICTIONARY_PATH)` | `-> Dict[str, Any]` | `:80` |
| `available()` | `() -> bool` | `:92` |
| `_score(question_tokens, obj)` | `-> int` | `:96` |
| `relevant_objects(question, limit=MAX_OBJECTS)` | `-> List[Dict]` | `:111` |
| `hint_for(question)` | `(str) -> str` | `:123` |
| `build_from_xlsx(path)` | `-> Dict[str, Any]` | `:151` |
| `build_from_csv(path)` | `-> Dict[str, Any]` | `:162` |
| `main(argv=None)` | `-> int` (CLI `python -m app.core.sf_dictionary`) | `:171` |

**Control flow** (request path `hint_for`)
1. `hint_for(question)` → `relevant_objects(question)` — `:129`.
2. `relevant_objects` calls `load()` — `:112`. `load` returns `_cache` if already set (`:83-84`); otherwise reads and `json.loads` the file (`:86`), and on **any** exception permanently caches `{"objects": {}}` (`:87-88`).
3. `_tokens(question)` — `:113`; empty token set short-circuits to `[]` (`:114-115`).
4. Every object in the dictionary is scored (`:116-118`): `_score` re-tokenises the object api+label (`:103`) and then **every field's api and label** (`:105-107`), 6 points per object-name hit, 1 per field hit.
5. Sorted by `(-score, api)` (`:119`) and truncated to `limit` (`:120`).
6. `hint_for` renders up to `MAX_FIELDS_PER_OBJECT` fields per object (`:134-139`), appends a `… +N more` suffix (`:140`), and wraps with a fixed instruction paragraph (`:142-148`).

CLI path `main` (`:171-188`): argparse → picks `build_from_xlsx` for `.xlsx`/`.xlsm` else `build_from_csv` (`:182`) → `save(data, args.out)` (`:184`) → prints object/field counts (`:187`).

**State & side effects**
- Filesystem write: `Path(path).parent.mkdir(parents=True, exist_ok=True)` and `Path(path).write_text(json.dumps(data))` in `save` — `:74-75`. Non-atomic (no temp-file + rename).
- Filesystem read: `Path(path).read_text` in `load` — `:86`; `open(path, ...)` in `build_from_csv` — `:165`; `openpyxl.load_workbook` in `build_from_xlsx` — `:155`.
- Global mutation: `_cache` written at `:77` (`save`) and `:86`/`:88` (`load`).
- Env read: `SF_DICTIONARY_PATH` at `:26-27` — read **at import time** into a module constant, so it is not re-read and not routed through `orchestrator/app/config.py`.
- No network egress, no DB, no GPU.

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/sql.py:97` (`from ..core.sf_dictionary import hint_for`), `:99` (`hint_for(question)`); `orchestrator/app/engines/live_sf.py:62`, `:68`; `orchestrator/tests/test_sf_dictionary.py:10`; documented operator command at `README.md:247` (`docker compose exec orchestrator python3 -m app.core.sf_dictionary /tmp/org.xlsx`).
- Outbound: `json`, `os`, `re`, `pathlib.Path`, `typing` (`:18-22`); `..config.settings` imported at `:24` but **never used** anywhere in the file; lazily `openpyxl` (`:153`), `csv` (`:163`), `argparse` (`:172`).

**Config**
- `SF_DICTIONARY_PATH` — `orchestrator/app/core/sf_dictionary.py:26`. Not present in `.env.example` (`rg -n 'SF_' .env.example` → only lines 13, 14, 18-21).
- No other env vars. `settings` import at `:24` is unused.

**Failure modes**
- `load()` (`:80-89`) catches **bare `Exception`** at `:87` and permanently memoises the empty dictionary. Missing file, corrupt JSON, permission error and `MemoryError` are all indistinguishable and all silent — there is no log line anywhere in the module.
- Because `_cache` is only ever set once by `load` (`:83-84`), a dictionary written *after* the orchestrator process started (which is exactly the documented workflow at `README.md:247`, run in a separate process) is never picked up. `save()` sets `_cache` at `:77` but only in the CLI process.
- `save` (`:74-75`) is non-atomic: an interrupted write leaves truncated JSON, which `load` then swallows into `{"objects": {}}`.
- `build_from_rows` (`:51-70`) indexes `row[3]`/`row[4]` guarded only by `len(row) > 2` at `:62` — a 3-column export raises `IndexError` at `:66-67`.
- `build_from_xlsx` (`:151-159`) passes a caller-supplied path straight to `openpyxl.load_workbook` with **no** `archive.check_zip_container` guard, unlike `orchestrator/app/core/profile.py:167` which explicitly re-asserts it. `wb[wb.sheetnames[0]]` raises `IndexError` on a zero-sheet workbook (`:156`).
- No bound on the dictionary size: `build_from_rows` accumulates every object/field into memory, `save` serialises it all, `load` parses it all and holds it for process lifetime.
- `main()` has no `try/except` — a bad path or unreadable workbook produces a raw traceback.

**Concurrency**
- Fully synchronous. `hint_for` is called from `async def _ask_sql` (`orchestrator/app/engines/sql.py:85`, call at `:99`) and `async def write_soql` (`orchestrator/app/engines/live_sf.py:61`, call at `:68`) — it runs **on the event loop**.
- `_cache` (`:44`) is unsynchronised module state; two concurrent `load()` calls can both read the file and both assign, harmless but duplicated.
- `_score` (`:96-108`) is O(objects × fields) *per question* and re-runs `_WORD_RE.findall` plus set construction on every field api and label with no memoisation. For an export with 1 000 objects × 200 fields that is ~400 000 regex scans and set allocations per request, synchronously blocking the loop.

**Complexity hotspots** — none over 60 LOC. Longest: `hint_for` `:123-148` (26 LOC), `build_from_rows` `:51-70` (20 LOC), `main` `:171-188` (18 LOC).

**Notable**
- Magic numbers: `MAX_OBJECTS = 4` (`:32`), `MAX_FIELDS_PER_OBJECT = 60` (`:33`), the `6 *` object-name weight (`:104`), hardcoded default path `/data/sf_dictionary.json` (`:27`).
- `available()` (`:92-93`) is used only by `orchestrator/tests/test_sf_dictionary.py:70` — no production caller (`rg -n` confirms).
- Dead import: `from ..config import settings` at `:24`.
- The `_STOP` list (`:37-42`) is hand-curated and includes `"name"`/`"names"`, so a question like "what is the account name field?" loses its most discriminating token.
- `_WORD_RE = r"[A-Za-z][A-Za-z0-9_]+"` requires ≥2 characters, so single-letter API prefixes never tokenise (`:35`).
- The walrus in the comprehension at `:117` (`if (s := _score(tokens, o)) > 0`) evaluates `_score` once per object — correct, but note the filter drops zero-score objects entirely so `hint_for` returns `""` rather than a wrong hint (asserted at `orchestrator/tests/test_sf_dictionary.py:59`, `:64`).

---

### orchestrator/app/core/repo_index.py  (54 LOC)

**Purpose** — Split cloned repository source files into overlapping line-windows (`CodeChunk`) so the repo Q&A engine can cite `path:Lstart-Lend`.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `CHUNK_LINES` | `= 60` | `orchestrator/app/core/repo_index.py:13` |
| `OVERLAP_LINES` | `= 10` | `:14` |
| `CodeChunk` | `@dataclass(path: str, start_line: int, end_line: int, text: str)` | `:17-22` |
| `chunk_file(path, text)` | `(str, str) -> List[CodeChunk]` | `:25` |
| `index_repo(repo_dir, max_chunks=6000)` | `(str, int) -> List[CodeChunk]` | `:46` |

**Control flow**
1. `index_repo` iterates `iter_source_files(repo_dir)` — `:49` (from `orchestrator/app/core/repo.py:221`).
2. For each file it calls `read_text(ap)` (`orchestrator/app/core/repo.py:238`) then `chunk_file(rel, text)` — `:50`.
3. `chunk_file` splits on `splitlines()` (`:27`), returns `[]` for an empty file (`:28-29`).
4. Window loop `:33-42`: `end = min(n, start + 60)` (`:34`), body is `"\n".join(lines[start:end]).strip()` (`:35`), appended only when non-empty (`:36-39`), `start = max(end - 10, start + 1)` (`:42`) — the `start + 1` guard prevents an infinite loop when `CHUNK_LINES <= OVERLAP_LINES`.
5. `index_repo` appends and returns early once `len(out) >= max_chunks` — `:51-53`.

**State & side effects** — pure in-memory; the only I/O is delegated to `repo.read_text` (`:50`). No DB, no network, no env reads. Result is persisted by the caller at `orchestrator/app/engines/repo.py:44-51` (`db.replace_repo_chunks`).

**Dependencies**
- Inbound: `orchestrator/app/engines/repo.py:17` (`from ..core.repo_index import chunk_file, index_repo`), `:42` (`index_repo(dest)`); `orchestrator/tests/test_repo.py:10`, `:36`, `:60`.
- Outbound: `dataclasses`, `typing` (`:8-9`), `.repo.iter_source_files` and `.repo.read_text` (`:11`). `Iterable`/`Tuple` are imported at `:9` but unused.

**Config** — none; `CHUNK_LINES`, `OVERLAP_LINES`, `max_chunks` are all hardcoded (`:13`, `:14`, `:46`). Nothing is env-tunable.

**Failure modes**
- `chunk_file` has **no character bound** — chunking is by *line count only* (`:34-35`). A file whose content is one very long line (minified JS/CSS, a single-line JSON) up to `_MAX_FILE_BYTES = 400_000` (`orchestrator/app/core/repo.py:47`) produces exactly one chunk of up to ~400 000 characters.
- `index_repo` caps chunk *count* at 6000 (`:46`) but not total characters: 6000 × 60 lines is unbounded in bytes.
- Nothing raises here; `read_text` already swallows `OSError` (`orchestrator/app/core/repo.py:239-243`).
- No timeout, no cancellation checkpoint inside the loop at `:49-53`.

**Concurrency** — fully synchronous, no module-level mutable state. Called from `async def _clone_and_index` (`orchestrator/app/engines/repo.py:28`, call at `:42`), so it reads and chunks up to `settings.repo_max_files` = 20 000 files **on the event loop**.

**Complexity hotspots** — none. `chunk_file` `:25-43` is 19 LOC.

**Notable**
- Unused imports `Iterable`, `Tuple` at `:9`.
- The `max_chunks=6000` early return (`:52-53`) truncates mid-file with no marker in the returned data and no signal to the caller that indexing was incomplete — `orchestrator/app/engines/repo.py:43-51` stores the truncated set as if complete.
- Docstring at `:1-5` says "the interface leaves room to swap in embeddings later"; retrieval is still pure keyword (`db.search_repo_chunks` at `orchestrator/app/engines/repo.py:120`).

---

### orchestrator/app/core/repo.py  (302 LOC)

**Purpose** — Detect a GitHub URL, shallow-clone it into a per-conversation workspace under quota/TTL, and build a language/tree/README overview. Cloned code is treated as data and never executed.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `_REPO_RE` | GitHub repo/tree/blob URL regex | `orchestrator/app/core/repo.py:23-27` |
| `_SKIP_DIRS` | 14-entry set | `:30-33` |
| `_TEXT_EXT` | 40-entry extension set | `:34-39` |
| `_LANG` | ext→language map | `:40-46` |
| `_MAX_FILE_BYTES` | `= 400_000` | `:47` |
| `RepoError(RuntimeError)` | class | `:50` |
| `GithubRef` | `@dataclass(owner, repo, ref=None, path=None)` | `:54-59`; `.key` `:61-63`; `.clone_url` `:65-67` |
| `detect_github(text)` | `(str) -> Optional[GithubRef]` | `:70` |
| `_dir_size_bytes(path)` | `(str) -> int` | `:83` |
| `enforce_quota_and_ttl()` | `() -> None` | `:94` |
| `workspace_path(conversation_id, ref)` | `(str, GithubRef) -> str` | `:122` |
| `_github_repo_size_kb(ref)` | `(GithubRef) -> Optional[int]` | `:130` |
| `shallow_clone(ref, dest)` | `(GithubRef, str) -> str` (commit SHA) | `:151` |
| `iter_source_files(repo_dir)` | generator `-> (rel, abs)` | `:221` |
| `read_text(path)` | `(str) -> str` | `:238` |
| `_ENTRY_HINTS` / `_CONFIG_HINTS` | tuples | `:246-249` / `:250-253` |
| `RepoOverview` | `@dataclass(tree, languages, readme, entry_points, key_configs, file_count)` | `:256-264` |
| `build_overview(repo_dir, max_tree_entries=200)` | `-> RepoOverview` | `:266` |

**Control flow** (primary path, driven from `orchestrator/app/engines/repo.py:28-52`)
1. `detect_github(request.text)` at `orchestrator/app/main.py:464-466` → `_REPO_RE.search` (`repo.py:72`); groups map to owner/repo/ref/path (`:75`); `path` kept only for `/blob/` URLs (`:76-77`).
2. `enforce_quota_and_ttl()` (`:94`): returns if `settings.workspace_dir` is absent (`:98-99`); `import time` **inside the function** (`:100`); iterates `os.listdir(base)` (`:104`), `shutil.rmtree` for anything older than `workspace_ttl_hours * 3600` (`:109-110`), otherwise collects `(mtime, path)` (`:111-112`); computes `total = sum(_dir_size_bytes(p) ...)` over every survivor (`:114`); then evicts oldest-first, calling `_dir_size_bytes(p)` **again** per victim (`:115-119`).
3. `workspace_path(conversation_id, ref)` (`:122-124`): `re.sub(r"[^A-Za-z0-9_.-]", "_", f"{conversation_id}__{owner}__{repo}")` joined onto `settings.workspace_dir`.
4. `shallow_clone(ref, dest)` (`:151`):
   a. `_github_repo_size_kb(ref)` (`:154` → `:130`) does a **synchronous** `httpx.get("https://api.github.com/repos/{owner}/{repo}", timeout=10.0)` (`:136-140`); 404 → `RepoError` (`:141-142`); any `httpx.HTTPError`/`ValueError`/`KeyError` → `None` (`:147-148`).
   b. Reject if `size_kb > settings.repo_max_mb * 1024` (`:155-159`).
   c. `shutil.rmtree(dest)` if it exists, `os.makedirs(dirname(dest))` (`:161-163`).
   d. Env hardening: `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/true`, `GIT_CONFIG_NOSYSTEM=1` (`:165-172`).
   e. `git -c core.hooksPath=/dev/null -c credential.helper= clone --depth 1 --no-tags --single-branch [--branch <ref>] <clone_url> <dest>` (`:173-180`), `subprocess.run(..., check=True, capture_output=True, timeout=180)` (`:182-184`).
   f. Timeout / non-zero exit → `rmtree` + `RepoError` with the last stderr line (`:185-191`).
   g. `shutil.rmtree(dest/.git/hooks)` (`:194`).
   h. Post-clone caps: file count via full `os.walk` (`:197`) vs `settings.repo_max_files` (`:198-203`); on-disk bytes via `_dir_size_bytes` (`:204`) vs `settings.repo_max_mb` (`:204-206`).
   i. `git -C dest rev-parse HEAD` with `timeout=15`; any `SubprocessError` → `sha = ""` (`:208-215`).
5. `build_overview(dest)` (`:266`): single pass over `iter_source_files` (`:272`) counting languages (`:275-277`), collecting up to 200 tree entries (`:278-279`), entry points (`:280-282`) and configs (`:283-284`); then probes four README names and reads the first 8000 chars (`:286-291`); sorts languages (`:293`) and returns (`:295-302`).
6. `iter_source_files` (`:221-235`): `os.walk`, prunes `_SKIP_DIRS` in place (`:224`), extension allow-list plus bare `dockerfile`/`makefile` (`:227`), skips files over 400 000 bytes (`:231-232`), swallows `OSError` on `getsize` (`:233-234`).

**State & side effects**
- **Network egress: `https://api.github.com/repos/{owner}/{repo}`** (`:137`) and **`git clone https://github.com/{owner}/{repo}.git`** (`:67`, `:180`). This is the only outbound internet dependency in the assigned set and directly contradicts the "fully local, air-gapped" framing — both hosts are hardcoded, unauthenticated, and not routed through the SSRF-guarded `core/net.py`.
- Filesystem writes/deletes: `shutil.rmtree` at `:110`, `:119`, `:162`, `:186`, `:189`, `:194`, `:199`, `:205`; `os.makedirs` at `:163`; the clone itself writes an entire repository into `settings.workspace_dir`.
- Process spawn: `subprocess.run(["git", ...])` at `:182` and `:209`.
- Env read: `dict(os.environ)` copied and mutated for the child at `:165-172`. The **entire parent environment is inherited by `git`**, including `SF_CLIENT_SECRET`, `HF_TOKEN` and every other secret in `.env`.
- No DB writes here (the caller does them at `orchestrator/app/engines/repo.py:43-51`), no GPU.

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/repo.py:15-16` (imports), `:32` `enforce_quota_and_ttl()`, `:33` `workspace_path`, `:35` `shallow_clone`, `:41` `build_overview`; `orchestrator/app/core/repo_index.py:11` (`iter_source_files`, `read_text`); `orchestrator/app/main.py:464-466` (`detect_github`); `orchestrator/app/uploads.py:86-88` (`enforce_quota_and_ttl`); `orchestrator/tests/test_repo.py:9`, `:14-33`, `:54`, `:68`, `:92`.
- Outbound: `os`, `re`, `shutil`, `subprocess`, `dataclasses`, `typing` (`:13-18`), `..config.settings` (`:20`), lazily `httpx` (`:133`) and `time` (`:100`).

**Config**
- `settings.workspace_dir` — `:95`, `:124`; env `WORKSPACE_DIR`, default `/data/workspaces`, `orchestrator/app/config.py:214`. **Not documented in `.env.example`.**
- `settings.workspace_ttl_hours` — `:109`; env `WORKSPACE_TTL_HOURS`, default 24, `orchestrator/app/config.py:215`; `.env.example:90`.
- `settings.workspace_quota_gb` — `:113`; env `WORKSPACE_QUOTA_GB`, default 20, `orchestrator/app/config.py:216`; `.env.example:91`.
- `settings.repo_max_mb` — `:155`, `:158`, `:204`, `:206`; env `REPO_MAX_MB`, default 300, `orchestrator/app/config.py:212`; `.env.example:88`.
- `settings.repo_max_files` — `:198`, `:202`; env `REPO_MAX_FILES`, default 20000, `orchestrator/app/config.py:213`; `.env.example:89`.
- Direct `os.environ` read at `:165` (copy for the subprocess).

**Failure modes**
- Raises `RepoError` at `:142`, `:156`, `:187`, `:191`, `:200`, `:206`.
- Swallowed: `OSError` on `getsize` in `_dir_size_bytes` (`:89-90`) and `iter_source_files` (`:233-234`); `httpx.HTTPError`/`ValueError`/`KeyError` in `_github_repo_size_kb` (`:147-148`) → returns `None` and the clone proceeds **with no pre-flight size check at all**; `subprocess.SubprocessError` in the `rev-parse` (`:213-214`) → the commit SHA is silently recorded as `""` and persisted by `db.save_repo` (`orchestrator/app/engines/repo.py:43`), destroying provenance.
- `shutil.rmtree(..., ignore_errors=True)` at `:110`, `:119`, `:162`, `:186`, `:189`, `:194`, `:199`, `:205` — a failed deletion is invisible, so quota enforcement can silently no-op forever.
- **The size caps are all post-hoc.** The pre-check at `:154-159` uses the GitHub API `size` field, which reports the *packed* repository size in KB. A repo of highly compressible content (repeated text, large generated files) reports a small packed size and expands to many GB on checkout. The on-disk check at `:204` runs only *after* the clone has finished writing, so the disk can already be full. The 180 s subprocess timeout (`:184`) is the only real bound on bytes written.
- `enforce_quota_and_ttl` (`:94-119`) has no error handling at all; `os.listdir`/`os.path.getmtime` raise on a vanished entry. `orchestrator/app/uploads.py:87-90` wraps it in `try/except Exception: pass`, but `orchestrator/app/engines/repo.py:32` does **not** — an `OSError` there aborts the clone.
- `subprocess.run(capture_output=True)` at `:182` buffers all of git's stdout/stderr in memory with no cap.
- No retry on the clone or on the GitHub API call; no backoff; no per-host rate limiting (`api.github.com` is 60 req/h unauthenticated for an IP — after that `raise_for_status` throws `httpx.HTTPStatusError`, caught at `:147`, so every repo silently skips the pre-check).
- `workspace_path` (`:122-124`) does not bound the resulting filename length; a long `conversation_id` yields `OSError: File name too long` from `os.makedirs` at `:163`.

**Concurrency**
- Fully synchronous module. Every function is called from `async def _clone_and_index` (`orchestrator/app/engines/repo.py:28`): `enforce_quota_and_ttl()` at `:32`, `shallow_clone()` at `:35`, `build_overview()` at `:41`. **The event loop is blocked for the full duration** — up to 180 s (clone timeout, `repo.py:184`) plus 10 s (GitHub API, `repo.py:138`) plus two full `os.walk` size scans plus a 20 000-file overview pass. Every other in-flight chat stream stalls.
- `orchestrator/app/uploads.py:88` calls `enforce_quota_and_ttl()` inside `async def create_upload` (`orchestrator/app/uploads.py:66`) — same blocking problem on every upload.
- No module-level mutable state, but the workspace directory is shared global state with **no locking**: two concurrent requests can `enforce_quota_and_ttl` simultaneously and evict a workspace another request is mid-clone or mid-read; `shutil.rmtree(dest)` at `:162` will happily delete a directory another conversation is reading (`iter_source_files` then yields paths that no longer exist and `read_text` returns `""` via `:242-243`).
- `_dir_size_bytes` is invoked twice per evicted entry (`:114` and `:118`), doubling an already O(files-on-disk) `stat` storm under a 20 GB quota.

**Complexity hotspots**
- `shallow_clone` — `orchestrator/app/core/repo.py:151-215`, **65 LOC**, ~12 branches (size pre-check, exists, timeout, called-process-error, file-count cap, byte cap, rev-parse failure). The largest function in the assigned set after `profile_tabular`.
- `build_overview` — `:266-302`, 37 LOC, 7 branches.
- `enforce_quota_and_ttl` — `:94-119`, 26 LOC, 5 branches, two nested O(n) walks.

**Notable**
- Magic numbers: `_MAX_FILE_BYTES = 400_000` (`:47`), clone `timeout=180` (`:184`), rev-parse `timeout=15` (`:211`), GitHub API `timeout=10.0` (`:138`), `max_tree_entries=200` (`:266`), README cap `8000` (`:290`), `entry_points[:10]` / `key_configs[:10]` (`:299-300`). None are configurable.
- `import time` inside `enforce_quota_and_ttl` (`:100`) instead of at module scope — inconsistent with the rest of the file.
- `_REPO_RE` (`:23-27`) allows `owner`/`repo` to be `.` or `..` and to start with `-`; these are neutralised only because they end up inside an `https://` URL (`:67`) and `workspace_path` sanitises the filesystem name (`:123`). `ref.ref` (group 3, `[^/\s]+`) is passed as the value of `--branch` (`:179`) — git's `parse-options` consumes the next argv unconditionally, so a leading `-` becomes a branch name rather than a new flag, but nothing in this module validates it.
- `.dockerfile` is in `_TEXT_EXT` (`:38`) *and* bare `dockerfile` is special-cased at `:227` — mild duplication.
- `_SKIP_DIRS` (`:30-33`) omits `.github`, `.terraform`, `coverage`, `.tox`, `bower_components`, and — most relevant to prompt size — does **not** skip minified bundles by name pattern.
- `RepoOverview.readme` is capped at 8000 chars (`:290`) but `build_overview` truncates again at the caller (`orchestrator/app/engines/repo.py:71` `ov.readme[:6000]`) — duplicated budget logic.
- `iter_source_files` yields `.json` (`:37`), so `package-lock.json` (typically one machine-generated file of hundreds of KB) is indexed as source.

---

### orchestrator/app/core/extract.py  (108 LOC)

**Purpose** — Turn a fetched HTML / PDF / plain-text body into readable text plus a title for the model; refuse anything else. HTML via trafilatura with a regex fallback; PDF via the `core/pdf.py` pypdfium2 text layer.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `_TITLE_RE`, `_TAG_RE`, `_WS_RE`, `_BLANKS_RE` | compiled regexes | `orchestrator/app/core/extract.py:17-20` |
| `UnsupportedContentError(ValueError)` | class | `:23` |
| `Extracted` | `@dataclass(title: str, text: str)` | `:27-30` |
| `_title_from_url(url)` | `(str) -> str` | `:33` |
| `_html_title(html)` | `(str) -> Optional[str]` | `:38` |
| `_strip_tags(html)` | `(str) -> str` | `:47` |
| `_extract_pdf_text(body)` | `(bytes) -> str` | `:55` |
| `extract_readable(content_type, body, url)` | `(str, bytes, str) -> Extracted` | `:64` |
| `truncate_chars(text, max_chars)` | `(str, int) -> str` | `:100` |

**Control flow**
1. `extract_readable` normalises the content type to its media type (`:69`) and lowercases the URL (`:70`).
2. **PDF branch** — `"pdf" in ct or lowered_url.endswith(".pdf")` (`:72`) → `_extract_pdf_text(body)` (`:73`). That function base64-encodes the whole body (`:60`), imports `render_pdf` from `orchestrator/app/core/pdf.py:27` (`:58`), and calls it with `max_pages=10`. `render_pdf` base64-**decodes** it again (`pdf.py:36`), opens the document, and for each of up to 10 pages extracts the text layer **and renders a full bitmap** at `RENDER_SCALE = 2.0` → PIL RGB → PNG → base64 data URL (`pdf.py:44-57`). `_extract_pdf_text` then discards the images into `_images` (`extract.py:60`) and returns only `text`.
3. **Plain-text branch** — exact match on `text/plain` / `text/markdown` (`:75-78`); body decoded with `errors="replace"`.
4. **HTML/XML/empty branch** (`:80-95`): decode (`:81`), try `trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True)` inside a bare `try/except Exception` (`:83-90`), fall back to `_strip_tags` when the result is falsy (`:91-92`), title from `_html_title` else the URL host (`:94`).
5. Anything else → `UnsupportedContentError(ct or "unknown")` (`:97`).

**State & side effects** — pure function of its arguments. No filesystem, no DB, no env reads, no network. Indirect CPU/memory pressure via pypdfium2 and lxml. No GPU.

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/url.py:47` (`extract.extract_readable`, called **inline** in `async def fetch_and_store` at `:27`), `:48` (`except extract.UnsupportedContentError`); `orchestrator/app/engines/search.py:328` (`extract.extract_readable` dispatched to `_EXTRACT_POOL`, a dedicated single-worker `ThreadPoolExecutor` at `search.py:60`), `:333` and `:370` (`extract.truncate_chars`); `orchestrator/tests/test_extract.py:16-58`; `orchestrator/tests/test_url_engine.py:49`; `orchestrator/tests/test_search_engine.py:78`.
- Outbound: `re`, `dataclasses`, `typing`, `urllib.parse.urlparse` (`:12-15`); lazily `base64` (`:56`), `.pdf.render_pdf` (`:58`), `trafilatura` (`:84`).

**Config** — **no env vars and no `settings` import**. Every bound is either hardcoded here (`max_pages=10` at `:60`) or lives in the caller (`settings.fetch_max_bytes`, default 5 000 000, `orchestrator/app/config.py:199`, applied at `orchestrator/app/engines/url.py:36` and `search.py:315`) or in `orchestrator/app/core/pdf.py` (`MAX_PDF_PAGES = 6` at `:18`, `RENDER_SCALE = 2.0` at `:19`, `MAX_TEXT_CHARS = 24000` at `:20`).

**Failure modes**
- Raises `UnsupportedContentError` at `:97` only.
- **`_extract_pdf_text` raises freely**: `binascii.Error` from `base64.b64decode` (`pdf.py:36`), `pypdfium2.PdfiumError` on a malformed/encrypted PDF (`pdf.py:37`), `MemoryError` on a large page (`pdf.py:50-53`), `ImportError` if the wheel is missing (`pdf.py:34`). `orchestrator/app/engines/url.py:48` catches **only** `UnsupportedContentError`, so any of these propagate out of `fetch_and_store` → `run_url_engine` (`url.py:91`, no try/except) → the generic handler at `orchestrator/app/main.py:670`, which converts the whole turn into a terminal `error` event carrying `str(exc)`. `orchestrator/app/engines/search.py:337-341` does catch bare `Exception`, so the search path degrades to the provider snippet.
- Swallowed: `except Exception: text = None` around trafilatura (`:89-90`) — hides `ImportError`, lxml parse aborts, and `MemoryError` alike, silently degrading to the much worse `_strip_tags` output with no signal.
- Content-type is trusted: a server sending `Content-Type: application/pdf` for 5 MB of arbitrary bytes forces the PDF path (`:72`); a `.pdf` **URL suffix** alone is enough even when the body is HTML.
- No timeout and no size bound inside this module — the only bound is the caller's `fetch_max_bytes`. `_strip_tags` (`:47-52`) runs three regex passes over up to 5 MB of markup.
- `truncate_chars` (`:100-108`) is safe: `rfind(" ")` fallback, no exceptions.

**Concurrency**
- Fully synchronous, no shared state.
- `orchestrator/app/engines/search.py:325-332` deliberately offloads to a **single-worker** executor and documents why (trafilatura's module-level compiled lxml XPath objects are not thread-safe — `search.py:318-324`).
- `orchestrator/app/engines/url.py:47` calls the same function **directly inside an `async def`**, so it (a) blocks the event loop and (b) can run trafilatura concurrently with the search pool's worker — the exact thread-safety hazard the search path was written to avoid.

**Complexity hotspots** — none. `extract_readable` `:64-97` is 34 LOC with 6 branches.

**Notable**
- `_extract_pdf_text` asks for `max_pages=10` (`:60`) while `orchestrator/app/core/pdf.py:18` sets `MAX_PDF_PAGES = 6` — the text path deliberately exceeds the vision path's budget, and then throws away everything the extra work produced.
- The `_images` variable at `:60` is assigned and never used — the entire raster pipeline (`pdf.py:50-57`: bitmap render at 2× scale, PIL RGB conversion, PNG encode, base64) is pure waste on this path.
- Magic numbers: `max_pages=10` (`:60`), `max_chars * 0.6` whitespace-boundary heuristic (`:106`).
- `ct == ""` is routed to the HTML branch (`:80`) — a server that omits `Content-Type` gets HTML treatment (deliberate; asserted at `orchestrator/tests/test_extract.py:34`).
- `text/markdown` is matched by exact equality (`:75`), so `text/x-markdown` or `application/json` fall through to the HTML branch or are refused.

---

### orchestrator/app/core/profile.py  (251 LOC)

**Purpose** — Profile an uploaded dataset (shape, dtypes, null rate, cardinality, ranges, capped sample) with DuckDB / openpyxl so the model is shown statistics rather than the file. Deliberately never reports min/max *values* for string columns.

**Public surface**
| Symbol | Signature | Line |
|---|---|---|
| `TABULAR_SUFFIXES` | `{".csv",".tsv",".txt",".parquet",".json",".jsonl",".ndjson"}` | `orchestrator/app/core/profile.py:30` |
| `EXCEL_SUFFIXES` | `{".xlsx"}` | `:31` |
| `clip(value)` | `(Any) -> Any` | `:34` |
| `_STRINGISH` | tuple of dtype tokens | `:45` |
| `_is_stringish(dtype)` | `(str) -> bool` | `:48` |
| `_duck()` | `() -> duckdb.DuckDBPyConnection` | `:52` |
| `_reader_sql(path)` | `(str) -> str` | `:73` |
| `profile_tabular(path, *, name=None)` | `-> Dict[str, Any]` | `:83` |
| `profile_excel(path, *, name=None)` | `-> Dict[str, Any]` | `:157` |
| `profile_file(path, *, name=None)` | `-> Dict[str, Any]` | `:208` |
| `profile_directory(root)` | `(str) -> List[Dict[str, Any]]` | `:230` |
| `profile_json(profiles)` | `(List[Dict]) -> str` | `:250` |

**Control flow** (upload path, driven from `orchestrator/app/uploads.py:121`)
1. `profile_directory(root)` walks the extraction directory (`:233`), sorts filenames (`:234`), stops at `settings.profile_max_files` (`:235-236`), and calls `profile_file` per file (`:240`).
2. `profile_file` (`:208-227`): refuses anything ending in `archive.REFUSED_SUFFIXES` (`:212-214`, set defined at `orchestrator/app/core/archive.py:38` = `.pkl .pickle .pkl.gz .xlsm .xlsb .pyc .so`); `.xlsx` → `archive.is_zip_container` check then `profile_excel` (`:215-218`); tabular suffix **or** `archive.sniff_format(path) == "parquet"` → `profile_tabular` (`:219-222`); otherwise a bare `{"kind": "other"}` stub (`:223-227`).
3. `profile_tabular` (`:83`): builds the output stub (`:86-90`), opens an in-memory DuckDB via `_duck()` (`:91`).
   - `_duck` (`:52-70`) connects to `:memory:` (`:55`) and applies three pragmas — `autoinstall_known_extensions=false`, `autoload_known_extensions=false`, `disabled_filesystems='HTTPFileSystem,S3FileSystem'` — **each wrapped in `try/except Exception: pass`** (`:61-69`). The comment (`:56-60`) explains that `enable_external_access=false` cannot be used because it would also block reading the local file.
   - `_reader_sql` (`:73-80`) picks `read_parquet` / `read_json_auto` / `read_csv_auto(..., SAMPLE_SIZE=20000, IGNORE_ERRORS=true)` and single-quote-escapes the path (`:75`).
   - `SELECT COUNT(*) FROM <src>` (`:94`) — full scan #1.
   - `DESCRIBE SELECT * FROM <src>` (`:95`) — scan #2; columns truncated to `settings.profile_max_columns` (`:98-100`).
   - **Per-column loop** (`:103-140`): identifier double-quoted and escaped (`:104`); one query for `COUNT(*) FILTER (WHERE col IS NULL), COUNT(DISTINCT col)` (`:107-109`); then either `MIN(LENGTH)/MAX(LENGTH)` for string-ish dtypes (`:119-123`) or `MIN/MAX` values for others (`:125-128`); then, when `0 < distinct <= 50`, a `GROUP BY … ORDER BY n DESC LIMIT settings.profile_top_values` (`:130-138`). Any per-column exception sets `stats_unavailable` (`:139-140`).
   - `SELECT * FROM <src> LIMIT settings.profile_sample_rows` (`:143-145`) and `clip`-ed into `sample_rows` (`:147-149`).
   - Whole body wrapped in `except Exception as exc: out["error"] = f"could not be read as a table: {type(exc).__name__}"` (`:150-151`); `con.close()` in `finally` (`:152-153`).
4. `profile_excel` (`:157-205`): **re-asserts** `archive.check_zip_container(path, label="spreadsheet")` (`:167`, raises `ArchiveError` on a bomb) before importing openpyxl (`:169`); `load_workbook(path, read_only=True, data_only=True)` (`:178`); iterates the first 10 worksheets (`:180`), takes the header row (`:182`), caps names at `profile_max_columns` (`:186`), then **iterates every remaining row** to count them while keeping only `profile_sample_rows` samples (`:189-194`); `wb.close()` in `finally` (`:203-204`).
5. `clip` (`:34-42`) passes numbers/bools through and truncates any other stringified value to `settings.profile_cell_chars` with a `…[truncated]` marker.

**State & side effects**
- Filesystem reads: `os.path.getsize` (`:88`, `:173`, `:226`), DuckDB reading the profiled file through `read_csv_auto`/`read_json_auto`/`read_parquet` (`:78-80`), `openpyxl.load_workbook` (`:178`), `os.walk` (`:233`).
- No filesystem writes, no DB writes here (the caller persists at `orchestrator/app/uploads.py:141-149` via `db.save_upload(... profiler.profile_json(profiles) ...)`).
- Network egress: **none by design** — `_duck` (`:52-70`) is the control that prevents DuckDB from reaching HTTP/S3, but see Failure modes.
- No GPU calls, no module-level mutable state, no direct env reads (all via `settings`).

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/uploads.py:26` (`from .core import archive, profile as profiler`), `:121` `profiler.profile_directory(extract_dir)`, `:147` `profiler.profile_json(profiles)`; `orchestrator/tests/test_dataset_profile.py:51,67,75,81,101,114,142,248`; `orchestrator/tests/test_archive_safety.py:204,302`.
- Outbound: `json`, `os`, `typing` (`:23-25`), `..config.settings` (`:27`), `. archive` (`:28`), lazily `duckdb` (`:53`) and `openpyxl.load_workbook` (`:169`).

**Config**
- `settings.profile_cell_chars` — `:41`; env `PROFILE_CELL_CHARS`, default 200, `orchestrator/app/config.py:185`.
- `settings.profile_max_columns` — `:98`, `:186`; env `PROFILE_MAX_COLUMNS`, default 60, `orchestrator/app/config.py:188`.
- `settings.profile_top_values` — `:134`; env `PROFILE_TOP_VALUES`, default 5, `orchestrator/app/config.py:186`.
- `settings.profile_sample_rows` — `:144`, `:191`; env `PROFILE_SAMPLE_ROWS`, default 5, `orchestrator/app/config.py:184`.
- `settings.profile_max_files` — `:235`; env `PROFILE_MAX_FILES`, default 40, `orchestrator/app/config.py:187`.
- Indirect: `settings.archive_max_uncompressed_mb` / `archive_max_ratio` via `archive.check_zip_container` (`:167`), `orchestrator/app/config.py:175-179`.
- None of the `PROFILE_*` vars appear in `.env.example` (`rg -n 'PROFILE_' .env.example` → no matches).

**Failure modes**
- Raises: `archive.ArchiveError` from `:167`; `openpyxl` exceptions from `:178` and the row iteration (`:189`); `OSError` from `os.path.getsize` at `:173` (unguarded, unlike `:88` and `:226` which test `os.path.exists` first).
- Swallowed, three separate bare `except Exception`:
  - `:68-69` — a failed **security pragma** is silently ignored. If DuckDB rejects `SET disabled_filesystems=...` (older build, different spelling), profiling runs with the HTTP and S3 filesystems live and no log line is emitted.
  - `:139-140` — every per-column statistic failure becomes `stats_unavailable = True` with no reason recorded.
  - `:150-151` — the whole tabular profile degrades to `{"error": "could not be read as a table: <ExceptionType>"}`; the message is discarded.
  - `:243-246` in `profile_directory` — "a bad file must not sink the upload"; only the exception class name is kept.
- No logging anywhere in the file — every one of the above is invisible in production.
- No timeout on any DuckDB query (`:94`, `:95`, `:107`, `:119`, `:125`, `:131`, `:143`) and no interrupt hook; a pathological file blocks until DuckDB finishes.
- No row-count bound on `profile_excel`'s counting loop (`:189-194`) — it visits every row of up to 10 sheets purely to produce `counted`.
- `read_csv_auto(..., IGNORE_ERRORS=true)` (`:80`) silently drops malformed rows, so `out["rows"]` (`:94`) under-reports and nothing in the profile tells the user rows were skipped.
- `_reader_sql` escapes `'` → `''` (`:75`), which is the correct DuckDB single-quote escape; filenames from an extracted archive therefore cannot break out of the literal. (Verified: DuckDB standard string literals have no backslash escape.)

**Concurrency**
- Fully synchronous. Called from `async def create_upload` (`orchestrator/app/uploads.py:66`, call at `:121`), so the **entire profiling run blocks the event loop** — every DuckDB scan and every openpyxl row.
- No module-level mutable state; each call gets a fresh `:memory:` connection (`:55`) closed in `finally` (`:153`). No race windows within the module.
- Nothing limits concurrent uploads, so N simultaneous uploads each open their own in-memory DuckDB.

**Complexity hotspots**
- `profile_tabular` — `orchestrator/app/core/profile.py:83-154`, **72 LOC**, the largest function in the assigned set. Cyclomatic complexity ≈ 12 (try/except/finally, `if len(described) > len(columns)`, per-column `try/except`, `_is_stringish` branch, `0 < distinct <= 50` branch, three ternaries at `:110`, `:122`, `:123`, plus two comprehensions).
- `profile_excel` — `:157-205`, 49 LOC, 6 branches.
- `profile_file` — `:208-227`, 20 LOC, 6 branches.

**Notable**
- **Repeated full scans**: `src` (`:93`) is an inline table function, not a materialised table, so *every* `con.execute` re-reads and re-parses the source file. With the default `profile_max_columns = 60` a CSV is scanned 2 (count + describe) + 60 × 2..3 (nulls/distinct, min/max, top values) + 1 (sample) ≈ **up to 183 full passes** over the file.
- Magic numbers: the `50`-distinct threshold for computing top values (`:130`), `worksheets[:10]` (`:180`), `SAMPLE_SIZE=20000` (`:80`).
- `_STRINGISH` (`:45`) includes `"BLOB"` and `"JSON"` — so a BLOB column reports byte-`LENGTH` statistics, which is correct behaviour but not what the comment (`:113-118`, "string column") describes.
- `TABULAR_SUFFIXES` includes `.txt` (`:30`), so any `.txt` in an upload is fed to `read_csv_auto`; a prose file yields a single-column profile rather than an error.
- The header comment block (`:1-20`) is the module's security contract: only `sample_rows` and `top_values` carry raw data, both through `clip`; string min/max *values* are deliberately absent (`:112-118`). The implementation matches the contract — verified line by line at `:119-128` and `:136-138`, and asserted by `orchestrator/tests/test_dataset_profile.py:101-142`.
- `profile_excel`'s `counted` (`:188`, `:190`, `:197`) is the only reason the whole sheet is traversed; nothing else uses the rows.
- `profile_json` (`:250-251`) serialises the full profile list with `default=str` and no size cap; the result is stored verbatim in SQLite by `orchestrator/app/uploads.py:147`.

---

## Cross-cutting observations

1. **Every module in this set is synchronous and every one is called from an `async def` handler.** Blocking work on the event loop: DuckDB introspection (`schema_cache.py:40-55` ← `engines/sql.py:189`), per-question dictionary scoring (`sf_dictionary.py:96-108` ← `engines/sql.py:99`), git clone + two `os.walk` size scans (`repo.py:151-215`, `:94-119` ← `engines/repo.py:32,35`), 20 000-file chunking (`repo_index.py:46-54` ← `engines/repo.py:42`), PDF rasterisation (`extract.py:55-61` ← `engines/url.py:47`), and up to 183 DuckDB file scans (`profile.py:83-154` ← `uploads.py:121`). Only `engines/search.py:325-332` offloads correctly.
2. **The "air-gapped" claim does not hold for `core/repo.py`**: `https://api.github.com` (`repo.py:137`) and `https://github.com` (`repo.py:67`, `:180`) are hardcoded, unauthenticated egress that bypasses the SSRF-guarded `core/net.py`.
3. **Silent-failure density**: 9 bare `except Exception` blocks across the set (`salesforce.py:169`, `sf_dictionary.py:87`, `repo.py:147`, `repo.py:213`, `extract.py:89`, `profile.py:68`, `:139`, `:150`, `:243`) and 8 `shutil.rmtree(..., ignore_errors=True)` calls. Not one of them logs.
4. **Config drift**: `SCHEMA_CACHE_TTL` is defined and never read (`config.py:265`); `SF_CLIENT_SECRET`, `SF_API_VERSION`, `SF_LIVE_TIMEOUT`, `SF_DICTIONARY_PATH`, `WORKSPACE_DIR` and all five `PROFILE_*` vars are consumed but absent from `.env.example`.
