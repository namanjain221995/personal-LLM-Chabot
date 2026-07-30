# orch-core-security — forensic evidence

Scope: `orchestrator/app/core/{__init__,sql_guard,net,archive,urls,report_paths,citations}.py`.
Every file below was read top-to-bottom with the Read tool. Line numbers are `cat -n` numbers from that read.
Cross-file claims were verified with `rg -n` and, where marked **[reproduced]**, by executing the pure module
in a throwaway interpreter (read-only; no app, no installs, no writes outside the scratchpad).

Total assigned LOC: **822** (`wc -l`: `__init__` 1, `sql_guard` 166, `net` 162, `archive` 292, `urls` 85,
`report_paths` 69, `citations` 47).

---

### orchestrator/app/core/__init__.py  (1 LOC)

**Purpose** — Package marker for `app.core`. Declares the contract for the whole package: *"Pure modules: no
network, no GPU, no heavy imports at module import time."* (`orchestrator/app/core/__init__.py:1`).

**Public surface** — none. No `__all__`, no re-exports, no symbols.

**Control flow** — none (docstring only).

**State & side effects** — none. Importing `app.core` executes nothing.

**Dependencies** —
- inbound: every `from ..core import X` / `from .core import X` in the app: `orchestrator/app/engines/sql.py:20`,
  `orchestrator/app/engines/sql.py:24`, `orchestrator/app/engines/search.py:25`,
  `orchestrator/app/engines/url.py:17`, `orchestrator/app/engines/url.py:18`,
  `orchestrator/app/engines/repo.py:15`, `orchestrator/app/engines/rag.py:21`,
  `orchestrator/app/engines/report.py:24`, `orchestrator/app/engines/agent.py:278`,
  `orchestrator/app/engines/live_sf.py:21`, `orchestrator/app/uploads.py:26`,
  `orchestrator/app/main.py:19`, `orchestrator/app/main.py:488`.
- outbound: none.

**Config** — none.

**Failure modes** — none.

**Concurrency** — n/a.

**Complexity hotspots** — none.

**Notable** — The package contract is **violated by two of its own members**:
`orchestrator/app/core/net.py:21` imports `httpx` at module scope (network library, though no I/O at import),
`orchestrator/app/core/archive.py:31` imports `..config.settings` at module scope (env-reading singleton),
and `orchestrator/app/core/urls.py:13` imports `..memory_recall` at module scope. `sql_guard`,
`report_paths` and `citations` do honour "stdlib only".

---

### orchestrator/app/core/sql_guard.py  (166 LOC)

**Purpose** — Regex + hand-written character scanner that must reduce any LLM-authored SQL to exactly one
read-only `SELECT` / `WITH … SELECT` before it reaches DuckDB. It is the only application-layer barrier
between prompt-injected model output and the warehouse.

**Public surface**
- `class SQLGuardError(ValueError)` — `sql_guard.py:23`.
- `_FORBIDDEN: re.Pattern` (module-level, effectively public constant) — `sql_guard.py:30-37`. Alternation:
  `insert|update|delete|attach|detach|copy|pragma|create|drop|alter|install|load|set|call|export|import|truncate|vacuum|merge|grant|revoke|checkpoint|use|begin|commit|rollback`, wrapped in `\b…\b`, `re.IGNORECASE`.
- `_STARTS_OK: re.Pattern = ^(select|with)\b` — `sql_guard.py:39`.
- `_FORBIDDEN_TABLE_FUNCS: re.Pattern` — `sql_guard.py:45-54`. Alternation:
  `read_csv_auto|read_csv|read_parquet|parquet_scan|parquet_metadata|parquet_schema|parquet_file_metadata|parquet_kv_metadata|read_json_auto|read_json_objects_auto|read_json_objects|read_json|read_ndjson_auto|read_ndjson_objects|read_ndjson|read_text|read_blob|read_xlsx|glob|sniff_csv|delta_scan|iceberg_scan|st_read`, each followed by `\s*\(`.
- `_scan(sql: str) -> Tuple[str, str]` — `sql_guard.py:57`. Returns `(cleaned, bare)`.
- `guard_sql(sql: str) -> str` — `sql_guard.py:129`. Raises `SQLGuardError`; returns the executable string.
- `is_safe_select(sql: str) -> bool` — `sql_guard.py:160`.

**Control flow** — `guard_sql`:
1. Null/blank reject — `sql_guard.py:135-136`.
2. `_scan(str(sql))` → `(cleaned, bare)` — `sql_guard.py:138`.
3. `core = bare.strip().rstrip(";").strip()`; empty ⇒ reject — `sql_guard.py:139-141`.
4. Stacked-statement check: `";" in core` ⇒ reject — `sql_guard.py:142-143`.
5. Prefix check: `_STARTS_OK.match(core)` ⇒ must start `select`/`with` — `sql_guard.py:144-145`.
6. Keyword blocklist on `core` — `sql_guard.py:146-148`.
7. Table-function blocklist on `core` — `sql_guard.py:149-154`.
8. Return `cleaned.strip().rstrip(";").strip()` — `sql_guard.py:156-157`. **The string that is executed is
   `cleaned`, but every check above ran on `bare`.** Divergence between the two is the whole attack surface.

`_scan` (`sql_guard.py:57-126`) is a single left-to-right character loop:
- `--` line comment: skip to `\n` inclusive, emit one space into `cleaned`, **nothing** into `bare` —
  `sql_guard.py:75-79`.
- `/* … */` block comment: skip to `*/` (unterminated ⇒ to EOF), one space into `cleaned`, nothing into `bare` —
  `sql_guard.py:81-85`. This is what catches `UPD/**/ATE` (the pieces reassemble in `bare`).
- `'…'` literal: quotes go to both buffers, body goes to `cleaned` only; `''` treated as an escaped quote —
  `sql_guard.py:87-103`.
- `"…"` identifier: same treatment, `""` escape — `sql_guard.py:106-122`.
- default: char to both — `sql_guard.py:123-125`.

**State & side effects** — none. No I/O, no globals mutated, no env reads.

**Dependencies** —
- inbound (verified `rg -n 'guard_sql|is_safe_select'`): `orchestrator/app/engines/sql.py:24` (import),
  `orchestrator/app/engines/sql.py:200` (first attempt), `orchestrator/app/engines/sql.py:205` (retry).
  `is_safe_select` has **no production caller** — only `orchestrator/tests/test_sql_guard.py:28,32,43,131`.
- outbound: `re`, `typing.Tuple`.

**Config** — none.

**Failure modes** — only `SQLGuardError`. Nothing is swallowed. No bound on input length: `_scan` is O(n) but
allocates two per-character lists, so a 50 MB model output costs ~3× that in RAM. `guard_sql` never rejects on
length, cost, or row/plan complexity — resource abuse is entirely out of its remit.

**Concurrency** — pure sync, no shared state, safe to call from any thread. Called from `async def
generate_and_run_sql` (`orchestrator/app/engines/sql.py:200`) — the guard itself is fast, but the DuckDB
execution that follows on `orchestrator/app/engines/sql.py:201` and `:206` is a **blocking** call made directly
in the coroutine (no `asyncio.to_thread`), see Failure modes of the caller below.

**Complexity hotspots** — `_scan` at `sql_guard.py:57-126` = **70 LOC**, ~17 decision points (largest function
in the entire assignment). `guard_sql` at `sql_guard.py:129-157` = 29 LOC, 8 decision points.

**Notable / measured bypasses** — parsing is **regex + ad-hoc scanner, not a real SQL parser**. The following
were executed against the real module and **passed the guard** [reproduced]:

| # | Input string that PASSES `guard_sql` | Why the guard misses it |
|---|---|---|
| B1 | `SELECT E'\'', * FROM read_csv('/etc/passwd')` | `_scan` does not model PostgreSQL/DuckDB `E'…'` backslash escapes. It reads `\` as a body char, then treats the following `''` as an *escaped quote* (`sql_guard.py:93-96`) and stays inside the literal, while DuckDB closes the literal at the same point. Everything after desynchronises: it lands in `cleaned` (executed) but is stripped from `bare` (checked). |
| B2 | `SELECT e'\'' AS a, * FROM accounts; ATTACH '/tmp/x.db' AS z` | Same desync — the `;` and `ATTACH` are inside the scanner's phantom string, so `sql_guard.py:142` and `:146` never see them. **Both the stacked-statement check and the keyword blocklist are defeated by one input.** |
| B3 | `SELECT E'\'' , 1 ; DROP TABLE accounts` | idem. |
| B4 | `SELECT (SELECT count(*) FROM '/etc/passwd') AS leak, * FROM accounts` | DuckDB replacement scans let a bare string literal in `FROM` name a file. No table-function token appears, and the literal body is stripped from `bare` by design (`sql_guard.py:87-103`), so neither blocklist can ever see the path. No trickery required. |
| B5 | `SELECT "read_text"('/etc/shadow') AS x, * FROM accounts` | `_FORBIDDEN_TABLE_FUNCS` matches bare identifiers only; a **quoted** function name has its body stripped from `bare` (`sql_guard.py:106-122`), leaving `""(` which matches nothing. DuckDB resolves quoted function names normally. |
| B6 | `SELECT * FROM accounts UNION ALL SELECT * FROM '/data/*.parquet'` | replacement scan + glob, same mechanism as B4. |
| B7 | `SELECT * FROM pragma_database_list()` / `SELECT * FROM duckdb_settings()` | `\bpragma\b` cannot match `pragma_database_list` (`_` is a word char, no boundary); `duckdb_*` metadata table functions are absent from the blocklist. Both are legal on a read-only connection and leak DB paths, temp dir and settings. (Rejected earlier by `references_a_known_table`, `orchestrator/app/engines/sql.py:173-176`, unless a real table is also referenced.) |
| B8 | `WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r) SELECT count(*) FROM accounts, r` | Unbounded recursive CTE. Guard has no notion of cost. |
| B9 | `SELECT count(*) FROM accounts a, accounts b, accounts c, accounts d, accounts e` | 5-way cartesian product. Guard has no notion of cost. |

What **IS** covered, correctly and verifiably:
- Comment-smuggled keywords (`UPD/**/ATE`, `DR--\nOP`) — `sql_guard.py:75-85` + `:146`.
- Keywords hidden inside ordinary string literals do **not** false-positive — `sql_guard.py:87-103`.
- Column/table names such as `update_date`, `payload`, `offset_value` do not false-positive (word boundaries) —
  confirmed by `orchestrator/tests/test_sql_guard.py:32`.
- Plain stacked statements `SELECT 1; DROP TABLE t` — `sql_guard.py:142`.
- Leading `INSERT/UPDATE/DELETE/ATTACH/COPY/PRAGMA/CREATE/DROP/ALTER/INSTALL/LOAD/SET/CALL/EXPORT/IMPORT/
  TRUNCATE/VACUUM/MERGE/GRANT/REVOKE/CHECKPOINT/USE/BEGIN/COMMIT/ROLLBACK` — `sql_guard.py:30-37` + `:144`.
- CTE that ends in a write (`WITH x AS (SELECT 1) INSERT INTO …`) — `sql_guard.py:146`,
  test `orchestrator/tests/test_sql_guard.py:137-138`.
- Un-quoted `read_csv(` / `read_parquet(` / `glob(` / `read_blob(` etc. incl. comment-split forms —
  `sql_guard.py:45-54` + `:149`.

What is **NOT** covered: `E'…'` / `e'…'` escape strings (B1–B3), dollar-quoted strings `$$…$$` (unmodelled by
`_scan` entirely — bodies leak into `bare` and produce *false positives* rather than bypasses), replacement
scans on string literals (B4, B6), quoted function names (B5), `duckdb_*`/`pragma_*` metadata functions (B7),
`sqlite_scan`/`postgres_scan`/`postgres_query`/`mysql_query`/`read_avro` (absent from the blocklist), any cost
or timeout bound (B8, B9), DuckDB `FROM`-first syntax (rejected — usability false-negative), and a leading
parenthesis `(SELECT …) UNION (SELECT …)` (rejected — usability false-negative).

**Defence in depth that actually saves this module** (read and verified):
`orchestrator/app/engines/sql.py:124-132` opens DuckDB with `read_only=True`,
`enable_external_access=False`, `autoinstall_known_extensions=False`, `autoload_known_extensions=False`. That
config is what stops B1/B4/B5/B6 from becoming arbitrary host-file reads and stops B2/B3 from writing. The
guard's own docstring claim at `sql_guard.py:9-13` ("rejects DuckDB filesystem/network table functions") is
**false as written** — the blocklist is one bypass deep.

---

### orchestrator/app/core/net.py  (162 LOC)

**Purpose** — The single SSRF choke point for every server-side fetch of a user-influenced URL (web search
result pages, pasted links). Resolves DNS before connecting, blocks private/reserved space, re-validates each
redirect hop, and caps time and body size.

**Public surface**
- `_MAX_REDIRECTS = 3` — `net.py:25`; `_ALLOWED_SCHEMES = {"http","https"}` — `net.py:26`.
- `class UnsafeURLError(ValueError)` — `net.py:29`.
- `class FetchError(RuntimeError)` — `net.py:33`.
- `@dataclass FetchResult(url: str, status: int, content_type: str, body: bytes)` — `net.py:37-42`.
- `_ip_is_blocked(ip: ipaddress._BaseAddress) -> bool` — `net.py:45`.
- `resolve_public_ips(host: str) -> List[str]` — `net.py:58`.
- `assert_url_is_fetchable(url: str) -> str` — `net.py:90`.
- `async safe_fetch(url: str, *, timeout_ms: int, max_bytes: int, accept: Optional[str] = None) -> FetchResult`
  — `net.py:103-109`.

**Control flow** — `safe_fetch`:
1. `await asyncio.to_thread(assert_url_is_fetchable, url)` — `net.py:121`. Return value discarded.
   1a. scheme allowlist — `net.py:94-95`; 1b. `parsed.hostname` non-empty — `net.py:96-98` (this also
   neutralises `http://trusted.example@127.0.0.1/`, since `hostname` drops userinfo and port);
   1c. `resolve_public_ips(host)` — `net.py:99`.
   1d. bare IP literal path — `net.py:66-73`; 1e. `socket.getaddrinfo(host, None)` — `net.py:76`;
   1f. **every** resolved address must be public — `net.py:82-86`.
2. Build `httpx.Timeout(connect=3.0, read=timeout_ms/1000.0, write=3.0, pool=2.0)` — `net.py:124-126`.
3. Fixed `User-Agent: TechSaraBot/1.0 (+local analytics)` — `net.py:127`; optional `Accept` — `net.py:128-129`.
4. New `httpx.AsyncClient(follow_redirects=False, …)` per call — `net.py:132-134`.
5. Loop `for _hop in range(_MAX_REDIRECTS + 1)` (≤4 requests, ≤3 redirects) — `net.py:135`.
6. `resp = await client.get(current)`; `httpx.HTTPError` → `FetchError` — `net.py:136-139`.
7. Redirect: require `Location` — `net.py:141-144`; resolve relative via `httpx.URL(current).join(location)` —
   `net.py:145`; **re-validate the new absolute URL** off-loop — `net.py:147`; continue.
8. `status >= 400` → `FetchError` — `net.py:150-151`.
9. Size cap: `resp.content[:max_bytes+1]`, reject if longer — `net.py:153-155`.
10. Return `FetchResult(current, status, content-type, resp.content[:max_bytes])` — `net.py:156-161`.
11. Loop exhausted → `FetchError("too many redirects")` — `net.py:162`.

**State & side effects** — **Network egress** to arbitrary attacker-chosen public hosts (that is the point).
DNS resolution via `socket.getaddrinfo` (`net.py:76`) on the default thread-pool executor. No DB writes, no
filesystem writes, no GPU. No module-level mutable state. No env reads (budgets are passed in by callers).

**Dependencies** —
- inbound (`rg -n 'safe_fetch|assert_url_is_fetchable|resolve_public_ips'`):
  `orchestrator/app/engines/search.py:25` (import), `orchestrator/app/engines/search.py:313` (`_fetch_source`);
  `orchestrator/app/engines/url.py:17` (import), `orchestrator/app/engines/url.py:34` (`fetch_and_store`),
  which catches `net.UnsafeURLError` at `orchestrator/app/engines/url.py:40` and `net.FetchError` at `:43`.
  Tests: `orchestrator/tests/test_net_ssrf.py`, `test_url_engine.py:8`, `test_search_engine.py:71`,
  `test_search_off.py:16`.
- outbound: `asyncio`, `ipaddress`, `socket`, `dataclasses`, `typing`, `urllib.parse.urlparse`, `httpx`
  (installed version **0.28.1**).

**Config** — no direct env reads. Callers inject `settings.fetch_timeout_ms` (`FETCH_TIMEOUT_MS`, default
**8000**, `orchestrator/app/config.py:198`) and `settings.fetch_max_bytes` (`FETCH_MAX_BYTES`, default
**5_000_000**, `orchestrator/app/config.py:199`) at `orchestrator/app/engines/url.py:36-37` and
`orchestrator/app/engines/search.py:315-316`.

**Failure modes** — Raises `UnsafeURLError` (blocked scheme/host/IP, unresolvable host) and `FetchError`
(transport error, ≥400, oversize, redirect without Location, too many redirects). Nothing is swallowed inside
`net.py`; callers swallow both into a status message (`orchestrator/app/engines/url.py:40-45`).
**No retry. No circuit breaker. No per-host or global concurrency limit. No total-request deadline** — the
`read` timeout is per-read, so a slowloris trickling one byte every 7 s keeps a connection alive indefinitely
within an 8 s read timeout. `socket.getaddrinfo` at `net.py:76` has **no timeout**; it merely runs on the
default executor, so a black-holed resolver parks a pool thread for the full resolver timeout and, at
`min(32, cpu+4)` workers, can starve every other `asyncio.to_thread` user in the process.

**Concurrency** — `safe_fetch` is async; the two blocking DNS calls are correctly off-loaded with
`asyncio.to_thread` (`net.py:121`, `net.py:147`) — the CHANGELOG entry at `CHANGELOG.md:99` documents this as a
fix for an event-loop stall. A brand-new `AsyncClient` per call means no connection reuse across fetches. No
shared mutable module state; no locks needed.

**Complexity hotspots** — `safe_fetch` at `net.py:103-162` = **60 LOC**, ~10 decision points. Everything else
is small.

**Coverage assessment (adversarial), verified [reproduced] with Python 3.12.3**

Blocked:
- Non-http(s) schemes (`file:`, `gopher:`, `ftp:`) — `net.py:94-95`; test `tests/test_net_ssrf.py:34-37`.
- IPv4 literals in private / loopback / link-local / reserved / multicast / unspecified space — `net.py:47-55`.
  `169.254.169.254 → blocked`, `0.0.0.0 → blocked`.
- **IPv6**: `::1 → blocked` (`is_loopback`), `fd00:ec2::254 → blocked` (`is_private`),
  `::ffff:127.0.0.1 → blocked` and `::ffff:169.254.169.254 → blocked` (Python maps `::ffff:0:0/96` into
  `is_private`). All confirmed by execution.
- **Alternate IPv4 encodings**: `ipaddress.ip_address` rejects `2130706433`, `0177.0.0.1`, `127.1` as literals,
  so they fall through to `getaddrinfo`, which resolves all three to `127.0.0.1` (confirmed by execution) and
  they are then blocked by the resolved-IP check at `net.py:82-86`. Decimal/octal/short-form encodings are
  therefore **covered**.
- Userinfo spoofing `http://public.example@127.0.0.1/` — `parsed.hostname` yields `127.0.0.1` (`net.py:96`).
- Multi-A-record rebinding (one name → public **and** private A records) — `net.py:82-86` rejects if *any*
  address is blocked; test `tests/test_net_ssrf.py:60-65`.
- Redirect into private space — re-validated at `net.py:147`; test `tests/test_net_ssrf.py:79-106`.
- Redirect chains longer than 3 — `net.py:135`, `net.py:162`.

Not blocked:
- **Sequential DNS rebinding (TOCTOU).** `resolve_public_ips` returns the validated IPs (`net.py:87`) and
  `assert_url_is_fetchable` throws them away (`net.py:99`). httpx then performs its **own, second** resolution
  when it connects at `net.py:137`. Nothing pins the address. See finding SSRF-1.
- **Carrier-grade NAT `100.64.0.0/10`.** On Python 3.12.3, `ipaddress.ip_address('100.64.1.1').is_private` is
  `False` (confirmed by execution), and `_ip_is_blocked` (`net.py:47-55`) tests no explicit network list, so
  `100.64.0.0/10` is fetchable. This is the range Tailscale hands out.
- **Body size is capped only after the whole body is in memory.** `client.get()` on httpx 0.28.1 buffers the
  entire response before returning (`net.py:137`); the cap at `net.py:153-155` runs afterwards. See FETCH-1.
- **No destination port restriction** — `http://public-host:6379/` is allowed.
- **No total deadline** (see Failure modes).
- `assert_url_is_fetchable` docstring says "Returns the normalized URL" (`net.py:91-92`) but returns `url`
  unchanged (`net.py:100`). No normalisation happens anywhere in the fetch path.

---

### orchestrator/app/core/archive.py  (292 LOC)

**Purpose** — Hostile-archive handling for dataset uploads: magic-byte sniffing, zip-slip prevention, symlink/
device rejection, four independent decompression-bomb caps, and depth-1 nesting. Also the pre-flight check for
`.xlsx`, which is itself a zip.

**Public surface**
- `_CHUNK = 64*1024` — `archive.py:33`; `_MAX_NAME_CHARS = 200` — `:34`; `_MAX_PATH_CHARS = 1024` — `:35`.
- `REFUSED_SUFFIXES = {".pkl",".pickle",".pkl.gz",".xlsm",".xlsb",".pyc",".so"}` — `archive.py:38`.
- `NESTED_ARCHIVE_SUFFIXES = {".zip",".tar",".gz",".tgz",".bz2",".xz",".7z",".rar"}` — `archive.py:41-43`.
- `class ArchiveError(Exception)` — `archive.py:46`.
- `@dataclass MemberPlan(name: str, size: int, compressed: int, is_nested_archive: bool = False)` — `:50-55`.
- `@dataclass ArchivePlan(members, total_uncompressed, nested_archives, skipped)` — `archive.py:58-63`.
- `_limits() -> Tuple[int,int,int]` — `archive.py:66`.
- `sniff_format(path: str) -> str` — `archive.py:74` (returns `zip|gzip|parquet|pdf|unknown`).
- `is_zip_container(path: str) -> bool` — `archive.py:92`.
- `safe_member_name(name: str) -> Optional[str]` — `archive.py:96`.
- `resolves_inside(root: str, relative: str) -> bool` — `archive.py:115`.
- `_classify(name: str) -> Optional[str]` — `archive.py:126`.
- `check_zip_container(path: str, *, label: str = "archive") -> ArchivePlan` — `archive.py:134`.
- `_write_member(src, dest_path: str, budget: List[int]) -> None` — `archive.py:192`.
- `extract_zip(path: str, dest: str) -> ArchivePlan` — `archive.py:212`.
- `extract_tar(path: str, dest: str) -> ArchivePlan` — `archive.py:233`.
- `extract(path: str, dest: str) -> ArchivePlan` — `archive.py:285`.

**Control flow** — `extract` (`archive.py:285-292`):
1. `sniff_format(path)` reads 8 magic bytes — `archive.py:287`, `:74-89`.
2. `zip` → `extract_zip` — `:288-289`; `gzip` **or** `tarfile.is_tarfile(path)` → `extract_tar` — `:290-291`;
   else `ArchiveError` — `:292`.

`check_zip_container` (`archive.py:134-189`):
1. `_limits()` reads the three settings — `:140`, `:66-71`.
2. `zipfile.ZipFile(path)` → `infolist()` — `:143-144`.
3. **member-count cap** — `:145-149`.
4. per member: skip dirs `:151-152`; **symlink reject** via `info.external_attr >> 16` + `stat.S_ISLNK`
   `:153-156`; `safe_member_name` `:157-160`; `_classify` refused-suffix `:161-164`; **per-member ratio cap**
   (only when `file_size > 1 MiB`) `:166-172`; **running header total vs cap** `:173-178`; nested-archive
   tagging `:179-183`; append `MemberPlan` `:184-186`.
5. `zipfile.BadZipFile` → `ArchiveError` — `:187-188`. **No other exception type is converted.**

`extract_zip` (`archive.py:212-230`):
1. `check_zip_container(path)` — `:214` (note: default label, so an `.xlsx` failure says "archive").
2. `os.makedirs(dest, exist_ok=True)` — `:215`; live byte budget list — `:216`.
3. per planned member: skip nested `:220-221`; `resolves_inside(dest, member.name)` **second zip-slip check**
   `:223-225`; `zf.open(member.name)` `:226`; `_write_member` `:227`.
4. `plan.members = extracted` — `:229`.

`_write_member` (`archive.py:192-209`): `os.makedirs(dirname)` `:194`; open `"wb"` `:195`; 64 KiB read loop
`:196-199`; decrement shared budget `:200`; on overflow close, `os.unlink(dest_path)`, raise `ArchiveError`
`:201-208`; else write `:209`.

`extract_tar` (`archive.py:233-282`): count cap `:243-247`; dirs skipped `:248-249`; **`if not member.isfile()`
rejects symlink, hardlink, char/block device and FIFO in one test** `:251-253`; `safe_member_name` `:254-257`;
`_classify` `:258-261`; nested archives recorded but **not** extracted `:262-265`; `resolves_inside` `:266-268`;
`tf.extractfile` `:269-271`; header total cap `:272-277`; `_write_member` `:278`; `tarfile.TarError` →
`ArchiveError` `:280-281`.

**State & side effects** — **Filesystem writes**: `os.makedirs` (`:194`, `:215`, `:237`), `open(dest,"wb")`
(`:195`), `os.unlink` (`:203`). **Filesystem reads**: `open(path,"rb")` (`:77`), `zipfile.ZipFile` (`:143`,
`:218`), `tarfile.open` (`:240`), `tarfile.is_tarfile` (`:290`). No network, no DB, no GPU. Reads
`settings.*` at call time (`:68-70`, `:177`, `:206`, `:216`, `:276`). No module-level mutable state.

**Dependencies** —
- inbound (`rg -n`): `orchestrator/app/uploads.py:26` (import), `:98` `is_zip_container`, `:99` `extract`,
  `:101` `sniff_format`, `:103` `extract`, `:109` `is_zip_container`, `:110` `check_zip_container`,
  `:122` `except archive.ArchiveError`; `orchestrator/app/core/profile.py:167` `check_zip_container`,
  `:212` `REFUSED_SUFFIXES`, `:216` `is_zip_container`, `:219` `sniff_format`, `:241` `except ArchiveError`.
  Tests: `orchestrator/tests/test_archive_safety.py`.
- outbound: `os`, `stat`, `tarfile`, `unicodedata`, `zipfile`, `dataclasses`, `typing`, `..config.settings`.

**Config** — via `settings`: `archive_max_uncompressed_mb` (`ARCHIVE_MAX_UNCOMPRESSED_MB`, default **2048**,
`orchestrator/app/config.py:175-177`) at `archive.py:68,177,206,216`; `archive_max_files`
(`ARCHIVE_MAX_FILES`, default **10000**, `config.py:178`) at `archive.py:69`; `archive_max_ratio`
(`ARCHIVE_MAX_RATIO`, default **200**, `config.py:179`) at `archive.py:70`.
`ARCHIVE_MAX_DEPTH` (`config.py:182`, default 1) is named in the module docstring (`archive.py:14`) but
**never read** — depth 1 is hard-coded by the `continue` at `archive.py:221` / `:265`.

**Failure modes** — Raises `ArchiveError` for: entry-count, ratio, header total, streaming-budget overflow,
bad ZIP, bad TAR, unsupported format. **Escapes uncaught** (only `BadZipFile`/`TarError` are converted):
- `KeyError` from `zf.open(member.name)` at `archive.py:226` whenever `safe_member_name` rewrote the name —
  **[reproduced]**: a zip whose entry is `./data//sales.csv` normalises to `data/sales.csv`,
  `check_zip_container` accepts it, and `extract_zip` raises
  `KeyError: "There is no item named 'data/sales.csv' in the archive"`. See finding ARCH-1.
- `RuntimeError` ("File … is encrypted, password required") and `NotImplementedError` ("compression type … not
  supported") from `zf.open` at `archive.py:226`.
- `NotADirectoryError` / `FileExistsError` from `os.makedirs` at `archive.py:194` when one member is a file
  named `a` and a later member is `a/b.csv`.
- `OSError` (ENOSPC, EACCES) from `_write_member`.
All of these are absorbed one frame up by the blanket `except Exception` at `orchestrator/app/uploads.py:128`,
which deletes the upload root and returns HTTP 400 with a generic message — so they degrade to a wrong status
code plus a lost upload, not a 500. `orchestrator/app/core/profile.py:243` has an equivalent blanket catch.
Nothing here has a timeout; an extraction of a 2 GiB archive is unbounded in wall-clock time.
Partial output is **not** cleaned up when the streaming budget trips at `archive.py:204` — only the current
member is unlinked (`:203`); earlier members stay on disk until `uploads.py:123/129` removes the root.

**Concurrency** — entirely synchronous and blocking. Called from `orchestrator/app/uploads.py:99/103/110`,
which is a FastAPI `async def` handler — so a multi-hundred-MB extraction runs **on the event loop**. No shared
mutable state; the `budget` list is per-call, so the byte cap is per-extraction and **not** global: N concurrent
uploads can each spend 2048 MB.

**Complexity hotspots** — `check_zip_container` `archive.py:134-189` = **56 LOC**, ~14 decision points.
`extract_tar` `archive.py:233-282` = **50 LOC**, ~15 decision points (highest branching in the file).
`extract_zip` = 19 LOC. `_write_member` = 18 LOC.

**Coverage assessment (adversarial)**

Covered:
- Absolute paths (`/etc/cron.d/x`), Windows drive paths (`C:\x`), UNC (`\\srv\s` → `//srv/s` → leading `/`),
  `..` at any depth, NUL and control characters, over-long names/paths — `archive.py:96-112`.
- Backslash-as-separator normalisation — `archive.py:102`.
- Unicode: NFC normalisation only (`archive.py:102`); NFC never produces an ASCII `/`, so it cannot manufacture
  a separator (NFKC would; it is not used).
- Zip symlinks: flagged and skipped from the Unix mode bits — `archive.py:153-156`. Even for a zip that carries
  **no** Unix attributes, escape is impossible because `_write_member` always creates a regular file with
  `open(dest,"wb")` (`archive.py:195`) — this extractor can never create a symlink, so no later member can be
  redirected through one.
- Tar symlinks, hardlinks, devices, FIFOs — one `not member.isfile()` test, `archive.py:251-253`.
- Second, resolved-path zip-slip check before every write — `archive.py:223` (zip) and `:266` (tar).
- Bombs: member count (`:145`, `:243`), per-member ratio (`:166-172`, zip only), header total (`:173-178`,
  `:272-277`), and a live streaming budget that does not trust the header (`:200-208`).
- Renamed files cannot pick their reader — `sniff_format` uses magic bytes (`archive.py:74-89`), used at
  `uploads.py:98/101/109` and `profile.py:216/219`.
- `.xlsx` is forced through the same caps before openpyxl sees it — `uploads.py:110`, re-asserted at
  `profile.py:167`.
- Nested archives are listed, never opened — `archive.py:220-221`, `:262-265`.
- Code-executing formats refused by suffix — `archive.py:38`, `:126-131`, `profile.py:212-214`.

Not covered:
- **Per-member compression ratio is never applied to tar/tar.gz.** `max_ratio` is unpacked at `archive.py:235`
  and then unused — a dead binding. Only the total cap and the streaming budget protect `.tar.gz`.
- **The ratio check exempts every member ≤ 1 MiB** — `archive.py:168` requires `info.file_size > 1024*1024`.
  The 2048 MB total cap is what actually bounds a many-small-members bomb.
- **The extraction byte budget is per-call, not per-user or global** — `archive.py:216`.
- **Nested archives are counted toward `total_uncompressed` but skipped at extraction** (zip), and for tar they
  are appended to `plan.members` at `archive.py:264` **without being written**, so the returned plan advertises
  files that do not exist on disk (zip replaces `plan.members` at `:229`; tar does not).
- **A plain `.csv.gz` (gzip of a non-tar) is misrouted**: `sniff_format` → `"gzip"` → `extract_tar` →
  `tarfile.open` raises `ReadError` → `ArchiveError("This archive is not a readable TAR file.")`
  (`archive.py:280-281`), a misleading rejection for a legitimate upload (`uploads.py:100-103`).
- No cap on total extraction wall-clock time; no cap on directory depth; no cap on the number of
  concurrent extractions.
- `resolves_inside` docstring claims the check is "re-checked at write time too" (`archive.py:118-120`); it is
  in fact evaluated once per member *before* the write (`:223`, `:266`), never inside `_write_member`.

**Notable** — Magic numbers: `_CHUNK` 64 KiB (`:33`), `_MAX_NAME_CHARS` 200 (`:34`), `_MAX_PATH_CHARS` 1024
(`:35`), the un-named `1024*1024` ratio exemption (`:168`), `wb.worksheets[:10]` in the sibling profiler
(`profile.py:180`). No TODO/FIXME/HACK markers anywhere in the assigned files (`rg -n 'TODO|FIXME|HACK|XXX'`
returns nothing).

---

### orchestrator/app/core/urls.py  (85 LOC)

**Purpose** — Finds pasted http(s) links in a user message and reduces a large fetched page to the portion most
relevant to the question, by keyword overlap, before it enters the model prompt.

**Public surface**
- `_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)` — `urls.py:16`.
- `_STRIP_TRAILING = ".,;:!?)]}\"'"` — `urls.py:17`.
- `extract_urls(text: str, limit: int = 5) -> List[str]` — `urls.py:20`.
- `chunk_text(text: str, chunk_chars: int = 1600, overlap: int = 200) -> List[str]` — `urls.py:32`.
- `select_relevant(text: str, query: str, max_chars: int) -> str` — `urls.py:52`.

**Control flow** — `extract_urls`: regex findall `:23` → `rstrip(_STRIP_TRAILING)` `:24` → dedupe,
order-preserving `:25-26` → break at `limit` `:27-28`.
`chunk_text`: short-circuit for small text `:34-35`; loop `:39-48` taking `chunk_chars`, backing up to the last
space after the 60 % mark `:41-44`, advancing `start = max(end - overlap, start + 1)` `:48` (progress is
guaranteed, no infinite loop); drop empties `:49`.
`select_relevant`: passthrough when `len(text) <= max_chars` `:59-60`; `keywords(query, max_keywords=12)` `:61`;
no keywords ⇒ head truncation `:62-63`; `chunk_size = min(1600, max(300, max_chars // 2))` `:66`; chunk `:67`;
score each chunk by `sum(low.count(k) for k in kws)` `:70-73`; sort by score desc `:75`; greedily fill the
budget `:78-82`; restore reading order `:83`; join with `"\n…\n"` `:84`; hard truncate `:85`.

**State & side effects** — none. Pure functions, no I/O, no globals mutated, no env reads.

**Dependencies** —
- inbound: `orchestrator/app/main.py:488` (import), `:490` `extract_urls(request.text, limit=settings.url_max_pages)`,
  `:499` `select_relevant(d["text"], request.text, 6000)`;
  `orchestrator/app/engines/url.py:18` (import), `:62` `select_relevant(d["text"], question, share)`.
  Tests: `orchestrator/tests/test_urls.py`.
- outbound: `re`, `typing`, **`..memory_recall.keywords`** (`urls.py:13`) — an app-level import, so `core.urls`
  is not the stdlib-only "pure module" the package docstring promises. `memory_recall` is itself light
  (`orchestrator/app/memory_recall.py:12-13` imports only `re` and `typing`).

**Config** — no direct env reads. `settings.url_max_pages` (`URL_MAX_PAGES`, default **5**,
`orchestrator/app/config.py:208`) is applied by the caller at `orchestrator/app/main.py:490`.

**Failure modes** — Nothing raises deliberately; nothing is caught. `extract_urls(None)` is safe (`text or ""`
at `:23`). `select_relevant` has no guard for `max_chars <= 0` — `chunk_size` would floor to 300 and the greedy
loop at `:79` (`total + len(c) > max_chars and picked`) picks exactly one chunk, then `joined[:0]` returns `""`.
No length cap on a single "URL": `_URL_RE` will happily match a 100 KB token, which is then handed to
`net.assert_url_is_fetchable`.

**Concurrency** — pure sync, thread-safe, no shared state. **Called inline from the async request path**
(`orchestrator/app/main.py:499`) and from `async def run_url_engine` via `_context_block`
(`orchestrator/app/engines/url.py:62`), so the chunk/score pass over up to `fetch_max_bytes` (5 MB) of
extracted text runs on the event loop. For a 5 MB page: ~3 200 chunks × 12 keywords × `str.count` — measurable
but not pathological.

**Complexity hotspots** — none. Largest function `select_relevant` = 34 LOC, ~7 decision points.

**Does urls.py feed net.py's checks?** Yes, but it performs **no** validation or normalisation of its own.
The chain is `main.py:490 extract_urls` → `main.py` routes to the url engine → `engines/url.py:34
net.safe_fetch` → `net.py:121 assert_url_is_fetchable`. `urls.py` does not lowercase the host, strip fragments,
percent-decode, punycode-encode, reject userinfo, or bound the URL length — every one of those is either
handled by `net.py:94-99` (scheme, hostname extraction, DNS) or not handled at all (length, fragment,
normalisation). Note `_URL_RE` deliberately stops at `)` and `]` (`urls.py:16`), so a legitimate URL containing
parentheses — e.g. `https://en.wikipedia.org/wiki/Salesforce_(company)` — is silently truncated to
`https://en.wikipedia.org/wiki/Salesforce_`.

**Notable** — `select_relevant`'s join separator `"\n…\n"` (`:84`) is not accounted for in the budget loop
(`:79`), so the result can overshoot `max_chars` before the final slice at `:85` fixes it. The scoring is
`str.count`, i.e. frequency-weighted substring matching, not token matching — the keyword `sale` scores inside
`wholesale`. Duplicated concept with `sync-worker/syncworker/chunking.py:14 chunk_text` (token-based, different
signature and semantics) — two independent chunkers in the monorepo.

---

### orchestrator/app/core/report_paths.py  (69 LOC)

**Purpose** — Resolves a user-supplied report filename inside `REPORTS_DIR` for `GET /reports/{filename}`, and
lists the directory for `GET /reports`.

**Public surface**
- `class ReportPathError(ValueError)` — `report_paths.py:19`.
- `resolve_report_file(reports_dir: str | Path, filename: str) -> Path` — `report_paths.py:23`.
- `list_reports(reports_dir: str | Path) -> List[dict]` — `report_paths.py:51`. Returns
  `{"filename", "size_bytes", "modified"}` per entry.

**Control flow** — `resolve_report_file`:
1. empty/whitespace reject — `:28-29`; `name = filename.strip()` `:30`.
2. `name in {".",".."} or ".." in name` reject — `:31-32`.
3. `"/" in name or "\\" in name` reject — `:33-34`.
4. `Path(name).is_absolute()` reject — `:35-36` (unreachable on POSIX after step 3).
5. leading `.` (hidden) reject — `:37-38`.
6. NUL reject — `:39-40`.
7. `base = Path(reports_dir).resolve()` `:42`; `resolved = (base / name).resolve()` `:43` — `resolve()` follows
   symlinks.
8. `resolved.is_relative_to(base)` else reject — `:46-47`. Returns `resolved` `:48`. Existence is **not**
   checked here (`:26`); the caller decides — `orchestrator/app/main.py:267-268`.

`list_reports`: missing dir ⇒ `[]` `:53-55`; `sorted(base.iterdir())` `:57`; skip non-files and dotfiles `:58-59`;
`p.stat()` `:60`; build dict with UTC ISO mtime `:61-67`; re-sort newest first `:68`.

**State & side effects** — filesystem **reads** only: `Path.resolve()` (`:42`, `:43`), `is_dir` (`:54`),
`iterdir` (`:57`), `is_file` (`:58`), `stat` (`:60`). No writes, no network, no DB, no env reads.

**Dependencies** —
- inbound: `orchestrator/app/main.py:19` (import), `:259` `list_reports(settings.reports_dir)` for
  `GET /reports` (`main.py:257-259`), `:265` `resolve_report_file(settings.reports_dir, filename)` for
  `GET /reports/{filename}` (`main.py:262-271`). Tests: `orchestrator/tests/test_report_paths.py`.
- outbound: `datetime`, `pathlib.Path`, `typing`.

**Config** — no direct env reads. `settings.reports_dir` (`REPORTS_DIR`, default **`/reports`**,
`orchestrator/app/config.py:100`) is injected by `main.py:259/265`.

**Failure modes** — Raises only `ReportPathError`, mapped to HTTP 400 at `orchestrator/app/main.py:266-267`.
`list_reports` has **no try/except**: a file removed between `iterdir()` (`:57`) and `stat()` (`:60`) raises
`FileNotFoundError` out of the route → HTTP 500. A `PermissionError` on `iterdir` likewise. No bound on the
number of entries returned — `list_reports` serialises the whole directory in one response.

**Concurrency** — pure sync, called from `async def reports_index` / `async def get_report`
(`orchestrator/app/main.py:257`, `:262`), so the directory scan runs on the event loop. TOCTOU window between
`resolve()` at `:43` and `FileResponse(path)` at `orchestrator/app/main.py:270` — only exploitable by something
that can already create symlinks inside `REPORTS_DIR`.

**Complexity hotspots** — none. `resolve_report_file` = 26 LOC, ~9 decision points; `list_reports` = 19 LOC.

**Coverage assessment (adversarial)** — Covered: `..`, `../../etc/passwd`, `..%2f` (the literal `..` substring
is rejected before any decoding — and FastAPI has already percent-decoded the path parameter), nested paths,
backslash paths, absolute paths, dotfiles, NUL bytes, and **symlink escape** (`resolve()` + `is_relative_to`,
`:43`/`:46`; test `orchestrator/tests/test_report_paths.py:49-55`). A symlink pointing *inside* `REPORTS_DIR`
is allowed and resolves to its target (test `:58-63`).
Not covered: **no extension allowlist** — every regular file in `REPORTS_DIR` is downloadable, and
`REPORTS_DIR` receives `.docx`/`.pdf` reports (`orchestrator/app/engines/report.py:256`) *and* CSV/XLSX exports
of warehouse query results (`orchestrator/app/engines/sql.py:407-409`). **No authentication or per-user
scoping** on either route — `orchestrator/app/main.py:55-56` states explicitly that "`/chat` and `/reports*`
remain auth-free", and neither `main.py:257-259` nor `:262-271` declares a `Depends`. See finding RPT-1.
The `".." in name` test at `:31` is a substring test, so a legitimate filename like `q3..final.pdf` is rejected
(harmless false positive, since generated names come from `slugify`).

---

### orchestrator/app/core/citations.py  (47 LOC)

**Purpose** — Turns RAG hits into `meta.citations` entries pointing at Salesforce Lightning record URLs.

**Public surface**
- `DEFAULT_LIGHTNING_BASE_URL = "https://techsara.lightning.force.com"` — `citations.py:11`.
- `record_url(record_id: str, base_url: str = DEFAULT_LIGHTNING_BASE_URL) -> str` — `citations.py:14`.
- `build_citation(record_id: str, object_name: Optional[str] = None, base_url: str = …) -> dict` —
  `citations.py:18-22`; returns `{"record_id", "object", "url"}` `:23-27`.
- `build_citations(hits: Iterable[Mapping], base_url: str = …) -> List[dict]` — `citations.py:30-33`.

**Control flow** — `build_citations`: iterate hits `:41`; `rid = hit.get("record_id")` `:42`; skip falsy or
already-seen `:43-44`; mark seen `:45`; `build_citation(str(rid), hit.get("object"), base_url)` `:46`;
`build_citation` → `record_url` → `f"{base_url.rstrip('/')}/{record_id}"` `:15`.

**State & side effects** — none. Pure, no I/O, no env reads, no globals mutated.

**Dependencies** —
- inbound: `orchestrator/app/engines/rag.py:21` (import), `:146` `build_citations(hits, base_url=settings.sf_lightning_base_url)`,
  `:148-150` (filters to record_ids the answer actually mentions, emits `meta.citations`);
  `orchestrator/app/engines/report.py:24` (import), `:205-208` (renders
  `f"[{c['record_id']}]({c['url']})"` **into the report markdown**);
  `orchestrator/app/engines/agent.py:278` (deferred import), `:285-286`, and merged at `:494-513`.
  Tests: `orchestrator/tests/test_citations.py`, `test_imports.py:10`.
- outbound: `typing` only.

**Config** — no direct env reads. Callers pass `settings.sf_lightning_base_url` (`SF_LIGHTNING_BASE_URL`,
default `https://techsara.lightning.force.com`, `.rstrip("/")` applied in config —
`orchestrator/app/config.py:103-105`).

**Failure modes** — `build_citations` requires each hit to be a `Mapping`; a non-mapping element raises
`AttributeError` (no `.get`), uncaught here. `record_url` performs **no validation and no percent-encoding** of
`record_id` — whatever is in the index is interpolated straight into a URL string (`:15`). Real Salesforce Ids
are 15/18-char alphanumerics, so the practical exposure is low, but nothing in this module enforces that.

**Concurrency** — pure sync, no shared state.

**Complexity hotspots** — none; largest function is 18 LOC.

**Notable** — `DEFAULT_LIGHTNING_BASE_URL` (`:11`) hard-codes a customer-specific hostname as the library
default; the configured `settings.sf_lightning_base_url` is only used because every caller passes it explicitly
(`rag.py:146`, `report.py:205`, `agent.py:285`). A caller that forgets the kwarg silently emits links to the
wrong org. The `object` field defaults to the literal `"Record"` (`:26`). No scheme validation on `base_url`,
so an operator setting `SF_LIGHTNING_BASE_URL=javascript:alert(1)//` produces `javascript:` hrefs that
`report.py:208` writes into markdown links.

---

## Cross-cutting observations

1. **The security core is guard-by-blocklist in two of three places.** `sql_guard` (`_FORBIDDEN`,
   `_FORBIDDEN_TABLE_FUNCS`) and `archive` (`REFUSED_SUFFIXES`) enumerate the bad; only `net` (`_ALLOWED_SCHEMES`)
   and `report_paths` enumerate the good. The two blocklists are the two modules with demonstrated bypasses.
2. **Every guard is backed by exactly one real enforcement layer, and each module's docstring overstates its own
   coverage.** `sql_guard.py:9-13`, `net.py:60-63`, `archive.py:118-120` and `net.py:91-92` all claim more than
   the code does. The system stays safe because of `orchestrator/app/engines/sql.py:124-132` (DuckDB
   `read_only` + `enable_external_access=false`) and because `_write_member` can only create regular files.
3. **Blocking work on the event loop.** `archive.extract*` (`uploads.py:99/103`), `sql._execute`
   (`engines/sql.py:201/206`), `report_paths.list_reports` (`main.py:259`) and `urls.select_relevant`
   (`main.py:499`) are all synchronous calls made directly inside `async def`. `net.py` is the only module in
   this set that correctly off-loads (`net.py:121`, `:147`).
4. **No module has a wall-clock budget.** Not `safe_fetch` (per-read timeout only), not `extract_*`, not the
   DuckDB execution the guard authorises.
