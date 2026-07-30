# `orchestrator/app/core/` — module reference

The `core` package is the orchestrator's pure-logic layer: guards, parsers, profilers, renderers and
protocol clients that the `engines/` layer composes. Its own package docstring states the contract —
*"Pure modules: no network, no GPU, no heavy imports at module import time"* —
[`__init__.py:1`](../../orchestrator/app/core/__init__.py#L1).

**The contract is violated by three of its own members.** `net.py:21` imports `httpx` at module
scope, `archive.py:31` imports the env-reading `..config.settings` singleton at module scope, and
`urls.py:13` imports `..memory_recall` at module scope. `sql_guard`, `report_paths` and `citations`
honour it exactly.

| Module | LOC | Role | Heaviest finding |
|---|---:|---|---|
| `sql_guard.py` | 166 | LLM-SQL barrier | `SEC-07`, `QUAL-02` |
| `net.py` | 162 | SSRF choke point | `SEC-03`, `PERF-02` |
| `archive.py` | 292 | Hostile-archive handling | — |
| `citations.py` | 47 | Lightning record URLs | — |
| `salesforce.py` | 268 | Live SF REST client | `REL-03` |
| `schema_cache.py` | 74 | Warehouse schema TTL cache | — |
| `sf_dictionary.py` | 192 | Org vocabulary → API names | `REL-03` |
| `repo_index.py` | 54 | Source → line-window chunks | — |
| `repo.py` | 302 | GitHub clone + overview | `SEC-05`, `REL-03`, `DATA-03` |
| `chart_spec.py` | 220 | Chart wire model + parser | — |
| `chart_data.py` | 113 | Deterministic histogram bins | — |
| `chart_decision.py` | 623 | Whether/how to chart | `REL-03` |
| `chart_pipeline.py` | 247 | Chart entry point | `REL-03` |
| `chart_profile.py` | 230 | Column shape inference | — |
| `charts_png.py` | 231 | matplotlib report renderer | — |
| `exports.py` | 125 | CSV/XLSX writer | `SEC-01` |
| `pdf.py` | 67 | PDF → page PNGs + text | `REL-01` |
| `extract.py` | 108 | HTML/PDF → readable text | `SEC-05`, `REL-03` |
| `profile.py` | 251 | Uploaded-dataset profiler | `REL-03` |
| `report_paths.py` | 69 | `/reports` path resolution | `SEC-01` |
| `urls.py` | 85 | URL extraction + relevance | `SEC-05` |
| **Total** | **3,926** | | |

LOC are `wc -l` counts taken directly. `__init__.py` (1 LOC) is excluded from the total.

Two structural facts hold across the whole package and are not repeated in every block:

1. **Every module here is synchronous, and almost every one is invoked directly from an `async def`.**
   The orchestrator runs one uvicorn process with no `--workers`
   ([`orchestrator/Dockerfile:52`](../../orchestrator/Dockerfile#L52)), so any blocking call stalls
   *all* concurrent SSE streams. `net.py` is the only module in the package that off-loads its
   blocking work (`net.py:121`, `net.py:147`).
2. **No `TODO`/`FIXME`/`HACK`/`XXX` marker exists in any file in this package** (verified `rg`).

---

## sql_guard

**Purpose** — Regex + hand-written character scanner that must reduce LLM-authored SQL to exactly one
read-only `SELECT`/`WITH … SELECT` before DuckDB sees it. The only application-layer barrier between
prompt-injected model output and the warehouse.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `SQLGuardError` | `class SQLGuardError(ValueError)` | [`sql_guard.py:23`](../../orchestrator/app/core/sql_guard.py#L23) |
| `_FORBIDDEN` | 27-keyword `re.Pattern`, `\b…\b`, `IGNORECASE` | `sql_guard.py:30-37` |
| `_STARTS_OK` | `^(select|with)\b` | `sql_guard.py:39` |
| `_FORBIDDEN_TABLE_FUNCS` | 22-entry table-function pattern, each `\s*\(` | `sql_guard.py:45-54` |
| `_scan` | `(sql: str) -> Tuple[str, str]` → `(cleaned, bare)` | `sql_guard.py:57` |
| `guard_sql` | `(sql: str) -> str` | [`sql_guard.py:129`](../../orchestrator/app/core/sql_guard.py#L129) |
| `is_safe_select` | `(sql: str) -> bool` | `sql_guard.py:160` |

**Control flow** — `guard_sql`:

1. Null/blank reject — `sql_guard.py:135-136`.
2. `_scan(str(sql))` returns two parallel buffers: `cleaned` (what will execute) and `bare` (what
   will be checked) — `sql_guard.py:138`.
3. `core = bare.strip().rstrip(";").strip()`; empty ⇒ reject — `sql_guard.py:139-141`.
4. Stacked-statement check: `";" in core` ⇒ reject — `sql_guard.py:142`.
5. Prefix check `_STARTS_OK.match(core)` — `sql_guard.py:144`.
6. Keyword blocklist on `core` — `sql_guard.py:146`.
7. Table-function blocklist on `core` — `sql_guard.py:149`.
8. Return `cleaned.strip().rstrip(";").strip()` — `sql_guard.py:156-157`.

`_scan` (`sql_guard.py:57-126`) is one left-to-right character loop: `--` comments skip to newline
emitting a space into `cleaned` and nothing into `bare` (`:75-79`); `/* … */` likewise (`:81-85`) —
this is what makes `UPD/**/ATE` reassemble in `bare` and get caught; `'…'` literals put the quotes in
both buffers and the body in `cleaned` only, treating `''` as an escaped quote (`:87-103`); `"…"`
identifiers get the same treatment with `""` escaping (`:106-122`).

**Step 8 is the entire attack surface: the string that executes is `cleaned`, but every check ran on
`bare`.**

**State & side effects** — None. No I/O, no globals, no env reads. Genuinely pure.

**Dependencies** — Inbound: [`engines/sql.py:24`](../../orchestrator/app/engines/sql.py#L24)
(import), `engines/sql.py:200` (first attempt), `engines/sql.py:205` (retry). `is_safe_select` has no
production caller — only `tests/test_sql_guard.py:28,32,43,131`. Outbound: `re`, `typing.Tuple`.

**Config** — None.

**Failure modes** — Raises only `SQLGuardError`; nothing swallowed. No input-length bound: `_scan`
allocates two per-character lists, so a 50 MB model output costs ~3× that in RAM. No notion of query
cost, row count or wall-clock — resource abuse is entirely outside its remit, and nothing else in the
SQL path supplies one (`engines/sql.py:117-139` sets no DuckDB interrupt or timeout).

**Concurrency** — Pure sync, thread-safe, no shared state. Called from `async def generate_and_run_sql`
(`engines/sql.py:200`); the guard itself is microseconds, but the DuckDB execution it authorises runs
inline on the event loop at `engines/sql.py:201` and `:206` (`PERF-01`).

**Complexity hotspots** — `_scan` [`sql_guard.py:57-126`](../../orchestrator/app/core/sql_guard.py#L57)
= **70 LOC, ~17 decision points** — the largest and most branch-dense function in the security core.
`guard_sql` `:129-157` = 29 LOC, 8 decision points.

**Findings** — `SEC-07` (confirmed E-string desynchronisation bypass; see
[security-model.md](security-model.md) §c for the payloads and the residual-risk statement),
`QUAL-02` (`is_safe_select` at `sql_guard.py:160` is dead). `DATA-01` and `PERF-01` live in the
caller (`engines/sql.py:201-207`) but are only reachable through this module's output.

---

## net

**Purpose** — The single SSRF choke point for every server-side fetch of a user-influenced URL.
Resolves DNS before connecting, blocks private/reserved space, re-validates each redirect hop, caps
time and body size.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_MAX_REDIRECTS` / `_ALLOWED_SCHEMES` | `3` / `{"http","https"}` | `net.py:25-26` |
| `UnsafeURLError` | `class(ValueError)` | `net.py:29` |
| `FetchError` | `class(RuntimeError)` | `net.py:33` |
| `FetchResult` | `@dataclass(url, status, content_type, body: bytes)` | `net.py:37-42` |
| `_ip_is_blocked` | `(ip) -> bool` | `net.py:45` |
| `resolve_public_ips` | `(host: str) -> List[str]` | `net.py:58` |
| `assert_url_is_fetchable` | `(url: str) -> str` | [`net.py:90`](../../orchestrator/app/core/net.py#L90) |
| `safe_fetch` | `async (url, *, timeout_ms, max_bytes, accept=None) -> FetchResult` | [`net.py:103`](../../orchestrator/app/core/net.py#L103) |

**Control flow** — `safe_fetch`:

1. `await asyncio.to_thread(assert_url_is_fetchable, url)` — `net.py:121`. **The return value is
   discarded.**
2. Inside: scheme allowlist `net.py:94-95`; `parsed.hostname` non-empty `net.py:96-98` (which also
   neutralises `http://trusted.example@127.0.0.1/`, since `hostname` drops userinfo);
   `resolve_public_ips(host)` `net.py:99` → bare-IP-literal path `net.py:66-73`, else
   `socket.getaddrinfo(host, None)` `net.py:76`, then **every** resolved address must be public
   `net.py:82-86`.
3. `httpx.Timeout(connect=3.0, read=timeout_ms/1000.0, write=3.0, pool=2.0)` — `net.py:124-126`.
4. Fixed `User-Agent: TechSaraBot/1.0 (+local analytics)` — `net.py:127`.
5. New `httpx.AsyncClient(follow_redirects=False, …)` per call — `net.py:132-134`.
6. `for _hop in range(_MAX_REDIRECTS + 1)` — ≤4 requests, ≤3 redirects — `net.py:135`.
7. Redirect: require `Location` `net.py:142-144`; join relative `net.py:145`; **re-validate the new
   absolute URL off-loop** `net.py:147`.
8. `status >= 400` → `FetchError` `net.py:150-151`.
9. Size cap `resp.content[:max_bytes+1]`, reject if longer — `net.py:153-155`.
10. Return `FetchResult` — `net.py:156-161`; loop exhausted → `FetchError("too many redirects")`
    `net.py:162`.

**State & side effects** — Network egress to arbitrary attacker-chosen public hosts (by design). DNS
via `socket.getaddrinfo` (`net.py:76`) on the default thread-pool executor. No DB, no filesystem, no
GPU, no module-level mutable state, no env reads.

**Dependencies** — Inbound: `engines/search.py:25`/`:313` (`_fetch_source`), `engines/url.py:17`/`:34`
(`fetch_and_store`, which catches `UnsafeURLError` at `url.py:40` and `FetchError` at `:43`). Tests:
`tests/test_net_ssrf.py`, `test_url_engine.py:8`, `test_search_engine.py:71`. Outbound: `asyncio`,
`ipaddress`, `socket`, `dataclasses`, `typing`, `urllib.parse.urlparse`, `httpx` 0.28.1.

**Config** — No direct env reads. Callers inject `settings.fetch_timeout_ms` (`FETCH_TIMEOUT_MS`,
default 8000, `config.py:198`) and `settings.fetch_max_bytes` (`FETCH_MAX_BYTES`, default 5 000 000,
`config.py:199`) at `engines/url.py:36-37` and `engines/search.py:315-316`.

**Failure modes** — Raises `UnsafeURLError` and `FetchError`; nothing is swallowed inside `net.py`.
**No retry, no circuit breaker, no per-host or global concurrency limit, no total-request deadline.**
The `read` timeout is per-read, so a slowloris trickling one byte every 7 s stays alive indefinitely
under an 8 s read timeout. `socket.getaddrinfo` (`net.py:76`) has no timeout; a black-holed resolver
parks a default-executor thread for the full resolver timeout and, at `min(32, cpu+4)` workers, can
starve every other `asyncio.to_thread` user in the process. `assert_url_is_fetchable`'s docstring
promises "the normalized URL" (`net.py:91-92`) but returns `url` unchanged (`net.py:100`) — no
normalisation happens anywhere in the fetch path.

**Concurrency** — `async`; the two blocking DNS calls are correctly off-loaded with `asyncio.to_thread`
(`net.py:121`, `:147`) — `CHANGELOG.md:99` documents this as a fix for an event-loop stall. A new
`AsyncClient` per call means no connection reuse. No shared mutable state.

**Complexity hotspots** — `safe_fetch` [`net.py:103-162`](../../orchestrator/app/core/net.py#L103) =
**60 LOC, ~10 decision points**. Everything else is small.

**Findings** — `SEC-03` (resolve-then-connect TOCTOU: `resolve_public_ips` returns validated IPs at
`net.py:87`, `assert_url_is_fetchable` throws them away at `net.py:99`, and httpx performs its own
second resolution when it connects at `net.py:137` — nothing pins the address). `PERF-02`
(`client.get()` on httpx 0.28.1 buffers the entire body before returning at `net.py:137`; the cap at
`net.py:153-155` runs afterwards). `SEC-05` (this is the pipe that carries untrusted web text toward
the prompt). Two further gaps have no assigned ID: carrier-grade NAT `100.64.0.0/10` is **not**
blocked (`ipaddress.ip_address('100.64.1.1').is_private` is `False` on Python 3.12.3 and
`_ip_is_blocked` at `net.py:47-55` lists no explicit networks) — that is the Tailscale range; and
there is no destination-port restriction, so `http://public-host:6379/` is allowed.

---

## archive

**Purpose** — Hostile-archive handling for dataset uploads: magic-byte sniffing, zip-slip prevention,
symlink/device rejection, four independent decompression-bomb caps, depth-1 nesting. Also the
pre-flight check for `.xlsx`, which is itself a zip.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_CHUNK` / `_MAX_NAME_CHARS` / `_MAX_PATH_CHARS` | `64*1024` / `200` / `1024` | `archive.py:33-35` |
| `REFUSED_SUFFIXES` | `{.pkl,.pickle,.pkl.gz,.xlsm,.xlsb,.pyc,.so}` | `archive.py:38` |
| `NESTED_ARCHIVE_SUFFIXES` | 8-entry set | `archive.py:41-43` |
| `ArchiveError` | `class(Exception)` | `archive.py:46` |
| `MemberPlan` / `ArchivePlan` | `@dataclass` | `archive.py:50-63` |
| `_limits` | `() -> Tuple[int,int,int]` | `archive.py:66` |
| `sniff_format` | `(path) -> "zip"\|"gzip"\|"parquet"\|"pdf"\|"unknown"` | `archive.py:74` |
| `is_zip_container` | `(path) -> bool` | `archive.py:92` |
| `safe_member_name` | `(name) -> Optional[str]` | `archive.py:96` |
| `resolves_inside` | `(root, relative) -> bool` | `archive.py:115` |
| `check_zip_container` | `(path, *, label="archive") -> ArchivePlan` | [`archive.py:134`](../../orchestrator/app/core/archive.py#L134) |
| `_write_member` | `(src, dest_path, budget: List[int]) -> None` | `archive.py:192` |
| `extract_zip` / `extract_tar` / `extract` | `(path, dest) -> ArchivePlan` | `archive.py:212` / `:233` / [`:285`](../../orchestrator/app/core/archive.py#L285) |

**Control flow** — `extract` (`archive.py:285-292`) reads 8 magic bytes via `sniff_format`, dispatches
`zip` → `extract_zip`, `gzip` **or** `tarfile.is_tarfile` → `extract_tar`, else `ArchiveError`.

`check_zip_container` (`:134-189`): `_limits()` reads three settings `:140`; `infolist()` `:143-144`;
member-count cap `:145-149`; per member — skip dirs `:151`, **symlink reject** via
`info.external_attr >> 16` + `stat.S_ISLNK` `:153-156`, `safe_member_name` `:157-160`, refused-suffix
`:161-164`, per-member ratio cap (only when `file_size > 1 MiB`) `:166-172`, running header total
`:173-178`, nested-archive tagging `:179-183`. `zipfile.BadZipFile` → `ArchiveError` `:187-188`; **no
other exception type is converted.**

`extract_zip` (`:212-230`): `check_zip_container` `:214`; live byte budget `:216`; skip nested `:220`;
**second, resolved-path zip-slip check** `resolves_inside(dest, member.name)` `:223`; `zf.open` `:226`;
`_write_member` `:227`. `_write_member` (`:192-209`) writes in 64 KiB chunks, decrements the shared
budget, and on overflow closes, `os.unlink`s the current file and raises.

`extract_tar` (`:233-282`): count cap `:243`; **`if not member.isfile()` rejects symlink, hardlink,
char/block device and FIFO in one test** `:251-253`; `safe_member_name` `:254`; `_classify` `:258`;
nested archives recorded but not extracted `:262-265`; `resolves_inside` `:266`; header total `:272`;
`_write_member` `:278`; `tarfile.TarError` → `ArchiveError` `:280-281`.

**State & side effects** — Filesystem writes: `os.makedirs` (`:194`, `:215`, `:237`), `open(dest,"wb")`
(`:195`), `os.unlink` (`:203`). Reads: `open(path,"rb")` (`:77`), `zipfile.ZipFile` (`:143`, `:218`),
`tarfile.open` (`:240`), `tarfile.is_tarfile` (`:290`). Reads `settings.*` at call time (`:68-70`,
`:177`, `:206`, `:216`, `:276`). No network, no DB, no GPU, no module-level mutable state.

**Dependencies** — Inbound: `uploads.py:26` (import), `:98` `is_zip_container`, `:99`/`:103` `extract`,
`:101` `sniff_format`, `:110` `check_zip_container`, `:122` `except ArchiveError`;
`core/profile.py:167` `check_zip_container`, `:212` `REFUSED_SUFFIXES`, `:216` `is_zip_container`,
`:219` `sniff_format`. Tests: `tests/test_archive_safety.py`. Outbound: `os`, `stat`, `tarfile`,
`unicodedata`, `zipfile`, `dataclasses`, `typing`, `..config.settings`.

**Config** — `archive_max_uncompressed_mb` (`ARCHIVE_MAX_UNCOMPRESSED_MB`, default 2048,
`config.py:175-177`) at `archive.py:68,177,206,216`; `archive_max_files` (`ARCHIVE_MAX_FILES`, default
10000, `config.py:178`) at `:69`; `archive_max_ratio` (`ARCHIVE_MAX_RATIO`, default 200,
`config.py:179`) at `:70`. **`ARCHIVE_MAX_DEPTH` (`config.py:182`) is named in the module docstring
(`archive.py:14`) but never read** — depth 1 is hard-coded by the `continue` at `:221`/`:265`.

**Failure modes** — Raises `ArchiveError` for entry-count, ratio, header-total and streaming-budget
overflow, bad ZIP/TAR, unsupported format. **Escapes uncaught** (only `BadZipFile`/`TarError` are
converted): `KeyError` from `zf.open(member.name)` at `:226` whenever `safe_member_name` rewrote the
name (a zip entry `./data//sales.csv` normalises to `data/sales.csv`, passes
`check_zip_container`, then raises `KeyError` in `extract_zip` — reproduced in the evidence pass);
`RuntimeError` ("encrypted, password required") and `NotImplementedError` ("compression type not
supported") from the same line; `NotADirectoryError`/`FileExistsError` from `os.makedirs` at `:194`
when one member is a file `a` and a later member is `a/b.csv`; `OSError` (ENOSPC, EACCES). All are
absorbed one frame up by the blanket `except Exception` at `uploads.py:128`, which deletes the upload
root and returns HTTP 400 — a wrong status code plus a lost upload rather than a 500. No wall-clock
bound: extracting 2 GiB is unbounded in time. Partial output is **not** cleaned up when the streaming
budget trips at `:204` — only the current member is unlinked.

**Concurrency** — Entirely synchronous and blocking, called from `async def create_upload`
(`uploads.py:66`, calls at `:99`/`:103`/`:110`) — a multi-hundred-MB extraction runs **on the event
loop**. The `budget` list is per-call, so the byte cap is per-extraction and **not** global: N
concurrent uploads can each spend 2048 MB.

**Complexity hotspots** — `check_zip_container`
[`archive.py:134-189`](../../orchestrator/app/core/archive.py#L134) = **56 LOC, ~14 decision points**;
`extract_tar` `:233-282` = **50 LOC, ~15 decision points** (highest branching in the file).

**Findings** — None of the catalogued IDs is charged to this module; it is the strongest guard in the
package (see [security-model.md](security-model.md) §c for the full covered/not-covered assessment).
`REL-03` applies to its consumers: everything that escapes here is swallowed by `uploads.py:128` and
`profile.py:243`. Unassigned gaps worth carrying to the report: the per-member ratio cap is never
applied to tar (`max_ratio` is unpacked at `:235` and then unused), the ratio check exempts every
member ≤ 1 MiB (`:168`), and a plain `.csv.gz` is misrouted to `extract_tar` and rejected with
"not a readable TAR file" (`:290-291` → `:280-281`).

---

## citations

**Purpose** — Turns RAG hits into `meta.citations` entries pointing at Salesforce Lightning record
URLs.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `DEFAULT_LIGHTNING_BASE_URL` | `"https://techsara.lightning.force.com"` | `citations.py:11` |
| `record_url` | `(record_id: str, base_url=DEFAULT_LIGHTNING_BASE_URL) -> str` | `citations.py:14` |
| `build_citation` | `(record_id, object_name=None, base_url=…) -> dict` | `citations.py:18-22` |
| `build_citations` | `(hits: Iterable[Mapping], base_url=…) -> List[dict]` | [`citations.py:30`](../../orchestrator/app/core/citations.py#L30) |

**Control flow** — `build_citations` iterates hits `:41`; `rid = hit.get("record_id")` `:42`; skips
falsy or already-seen `:43-44`; marks seen `:45`; delegates to `build_citation` `:46`, which returns
`{"record_id", "object", "url"}` `:23-27` with `url = f"{base_url.rstrip('/')}/{record_id}"` `:15`.

**State & side effects** — None. Pure; no I/O, no env reads, no global mutation.

**Dependencies** — Inbound: `engines/rag.py:21`/`:146` (filtered at `:148-150` to record_ids the answer
actually mentions, then emitted as `meta.citations`); `engines/report.py:24`/`:205-208`, which renders
`f"[{c['record_id']}]({c['url']})"` **into the report markdown**; `engines/agent.py:278` (deferred
import), `:285-286`, merged at `:494-513`. Tests: `tests/test_citations.py`, `test_imports.py:10`.
Outbound: `typing` only.

**Config** — No direct env reads. Callers pass `settings.sf_lightning_base_url`
(`SF_LIGHTNING_BASE_URL`, `.rstrip("/")` applied in `config.py:103-105`).

**Failure modes** — A non-`Mapping` element raises `AttributeError`, uncaught here. `record_url`
performs **no validation and no percent-encoding** of `record_id` (`:15`) — whatever is in the LanceDB
index is interpolated straight into a URL. Real Salesforce Ids are 15/18-char alphanumerics, so the
practical exposure is low, but nothing in this module enforces that. No scheme validation on
`base_url`: an operator setting `SF_LIGHTNING_BASE_URL=javascript:alert(1)//` produces `javascript:`
hrefs that `report.py:208` writes into markdown links.

**Concurrency** — Pure sync, no shared state.

**Complexity hotspots** — None. Largest function is 18 LOC.

**Findings** — None of the catalogued IDs. Two unassigned observations for the report:
`DEFAULT_LIGHTNING_BASE_URL` (`citations.py:11`) hard-codes a customer-specific hostname as the
library default — it is only harmless because all three callers pass the configured value explicitly;
and the missing `base_url` scheme check above is a stored-XSS-shaped hazard in the report renderer.

---

## salesforce

**Purpose** — Live read-only Salesforce REST access (OAuth client-credentials → SOQL `/query` and
describe APIs) used when the synced DuckDB warehouse is stale or lacks the object. Also guards
model-generated SOQL and merges live rows over warehouse rows.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `MAX_ROWS` | `= 200` | `salesforce.py:29` |
| `_FORBIDDEN` | 10-keyword SOQL write/DDL pattern | `salesforce.py:33-36` |
| `SalesforceUnavailable` / `UnsafeSoql` | `class(RuntimeError)` / `class(ValueError)` | `salesforce.py:39` / `:43` |
| `configured` | `() -> bool` | `salesforce.py:47` |
| `guard_soql` | `(soql: str) -> str` | `salesforce.py:55` |
| `_Token` / `_token` | class, `TTL = 25*60`; module-global instance | `salesforce.py:93`, `:97`, `:108` |
| `_authenticate` | `async () -> Tuple[str, str]` | `salesforce.py:111` |
| `run_soql` | `async (str) -> Tuple[str, List[Dict]]` | [`salesforce.py:144`](../../orchestrator/app/core/salesforce.py#L144) |
| `_get` / `list_objects` / `describe_object` | `async` | `salesforce.py:177` / `:191` / `:211` |
| `_clean` / `merge_rows` | sync | `salesforce.py:229` / `:234` |

**Control flow** — `run_soql`:

1. `guard_soql(soql)` — `:146`. Rejects empty `:60-61`, whitespace-normalises and `rstrip(";")`
   `:62-63`, rejects any remaining `;` `:65-66`, requires a leading `SELECT` `:67-68`, scans
   `_FORBIDDEN` `:69-71`, requires a `FROM` `:72-73`.
2. `SELECT COUNT()` special case: a trailing `LIMIT n` is *stripped* and the query returned **uncapped**
   `:79-80`. Otherwise a trailing `LIMIT n` is lowered to `MAX_ROWS` if larger `:84-87`, or ` LIMIT 200`
   is appended `:88-89`.
3. `_authenticate()` `:147` — returns the cached token if `not _token.stale()` `:114-115`, else
   requires `settings.sf_client_secret` `:117-122` and POSTs `grant_type=client_credentials` with a
   hard-coded `timeout=30.0` `:123-131`. Non-200 → `SalesforceUnavailable` with the status only, body
   deliberately not echoed `:132-136`. `body["access_token"]`/`body["instance_url"]` read with bare
   subscripting `:137-141`.
4. GET `{instance}/services/data/{sf_api_version}/query?q=…` with `timeout=settings.sf_live_timeout`
   `:148-153`; **on HTTP 401 the cached token is cleared and the GET retried exactly once** `:154-162`.
5. Other non-200: the response JSON's `[0]["message"][:200]` is spliced into the exception `:163-171`.
6. `_clean` applied to *all* records, then sliced to `MAX_ROWS` `:173-174`.

**State & side effects** — Network egress: token POST `:124-131`, query GET `:149-153`/`:158-162`,
describe/sobjects GET `:181-185`. **`instance_url` is whatever the token endpoint returns (`:139`) —
the egress host is server-supplied, not pinned.** Global mutation of `_token.value/.instance/.at`
`:138-140` and `_token.value = None` `:155`. No DB, no filesystem, no GPU.

**Dependencies** — Inbound: `engines/live_sf.py:21`/`:113`/`:145`/`:121`/`:129`;
`engines/agent.py:289-290`, `:315`, `:304`/`:329` (`merge_rows`); `engines/sql.py:304`/`:306`
(`configured()`). Tests: `tests/test_live_salesforce.py:10,212`. Outbound: `re`, `time`, `typing`,
`httpx`, `..config.settings`.

**Config** — `SF_CLIENT_ID` (`config.py:118`), `SF_CLIENT_SECRET` (`:119`), `SF_LOGIN_URL` (`:120`),
`SF_PRIVATE_KEY_B64` (`:121`), `SF_API_VERSION` (default `v61.0`, `:122`), `SF_LIVE_TIMEOUT` (default
45 s, `:123`), all via `settings`. The gating flag `settings.sf_live_enabled` (`config.py:124`) is
**not** consulted here — only by `engines/sql.py:306`. `.env.example:18-21` documents `SF_CLIENT_ID` /
`SF_USERNAME` / `SF_LOGIN_URL` / `SF_PRIVATE_KEY_B64` but **not** `SF_CLIENT_SECRET`, which is the only
variable `_authenticate` can actually use (`salesforce.py:117-122`).

**Failure modes** — Raises `UnsafeSoql` (`:61,66,68,71,73,214`) and `SalesforceUnavailable`
(`:113,120,134,171,187`). **Unwrapped exceptions escape**: `httpx.ConnectError/ConnectTimeout/
ReadTimeout/RemoteProtocolError` from `:124`, `:150`, `:159`, `:181`; `json.JSONDecodeError` from
`:137`/`:173`/`:188`; `KeyError` from `:138-139` if the token endpoint returns a 200 with a different
shape. Swallowed: bare `except Exception: pass` around the error-detail parse `:169-170` (benign). No
retry/backoff on `_authenticate` for 5xx or network failure. `_get` (`:177-188`) has **no 401 retry**
unlike `run_soql`, so an expired cached token makes `list_objects`/`describe_object` fail hard. No
response-body size bound — `resp.json()` at `:173` materialises an arbitrarily large response before
any capping. No circuit breaker; every failure costs the full 45 s.

**Concurrency** — All network functions are `async` and use `httpx.AsyncClient` correctly. **A new
`AsyncClient` is constructed per call** (`:123`, `:148`, `:157`, `:180`) — no pooling, TLS handshake
per query; same defect class as `PERF-04`, different module. `_token` (`:108`) is shared module-level
mutable state with **no `asyncio.Lock`**: (a) N concurrent questions all see `stale()` and all POST the
token endpoint; (b) request A's `_token.value = None` at `:155` forces request B to re-authenticate;
(c) two writers interleaving `:138-140` can leave `value` and `instance` from different responses.
`TTL = 25*60` (`:97`) is a fixed client-side guess; the org's session timeout can be shorter.

**Complexity hotspots** — None over 60 LOC. Largest: `guard_soql` `:55-90` (36 LOC, 8 branches),
`merge_rows` `:234-268` (35 LOC), `run_soql` `:144-174` (31 LOC).

**Findings** — `REL-03` (bare `except Exception: pass` at `:169-170`, no log). Unassigned but
report-worthy: `configured()` (`:47-52`) returns `True` when only `sf_private_key_b64` is set while
`_authenticate` then refuses at `:117-122` — the JWT grant is deliberately unimplemented, so
`configured()` over-reports; the `SELECT COUNT()` branch (`:79-80`) is the one path that returns an
uncapped query; and the `;`/`_FORBIDDEN` checks run over string literals too, so a legitimate
`WHERE Name = 'a;b'` or `WHERE Stage__c = 'Merge'` is rejected.

---

## schema_cache

**Purpose** — TTL cache of `{table: [(column, dtype), …]}` read from a read-only DuckDB connection,
used to ground the text-to-SQL prompt.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `SchemaCache` | `class`, `__init__(ttl_seconds: float = 300.0)` | `schema_cache.py:11`, `:14` |
| `SchemaCache.get` | `(db_path: str, force_refresh: bool = False) -> Dict[str, List[Tuple[str,str]]]` | [`schema_cache.py:18`](../../orchestrator/app/core/schema_cache.py#L18) |
| `SchemaCache.invalidate` | `(db_path: str \| None = None) -> None` | `schema_cache.py:27` |
| `SchemaCache._load` | `@staticmethod (db_path) -> Dict[...]` | `schema_cache.py:33-34` |
| `format_schema` | `(schema) -> str` | `schema_cache.py:65` |
| `schema_cache` | module-level singleton | `schema_cache.py:74` |

**Control flow** — `get(db_path)` reads `time.monotonic()` `:19`; returns the cached value when
present, not force-refreshed and inside TTL `:20-22`; else `_load(db_path)` `:23`. `_load` lazily
imports duckdb `:35` and connects with `read_only=True`, `enable_external_access=False`,
`autoinstall_known_extensions=False`, `autoload_known_extensions=False` `:40-48`; queries
`information_schema.columns WHERE table_schema='main' ORDER BY table_name, ordinal_position` `:50-55`;
closes in `finally` `:56-57`; folds rows into the dict `:59-62`; result stored with a timestamp `:24`.

**State & side effects** — Filesystem read of the DuckDB file (`:40`), read-only. Global mutation of
`self._cache` on the module singleton (`:24`, `:29`, `:31`). No network, no GPU, no env reads.

**Dependencies** — Inbound: `engines/sql.py:23` (import), `:189` `format_schema(schema_cache.get(...))`,
`:191` `schema_cache.get(...)`; `engines/live_sf.py:22`/`:56`. Tests: `tests/test_imports.py:13`,
`test_live_salesforce.py:257`. Outbound: `time`, `typing`, lazily `duckdb`.

**Config** — Consumes no env vars directly; `db_path` is always `settings.duckdb_path` (`DUCKDB_PATH`,
default `/data/warehouse.duckdb`, `config.py:96`). **`config.py:265` defines
`schema_cache_ttl = _float("SCHEMA_CACHE_TTL", 300.0)` and nothing ever reads it** — repo-wide `rg` for
`schema_cache_ttl|SCHEMA_CACHE_TTL` returns only that line. The singleton at `:74` takes the hard-coded
300.0.

**Failure modes** — `duckdb.connect` (`:40`) raises `duckdb.IOException` if the warehouse is missing,
locked by a writer with a different config, or corrupt; `import duckdb` (`:35`) raises `ImportError`.
Neither is caught anywhere in this module — both propagate to `engines/sql.py:189`. No query timeout, no
retry, no bound on the number of tables/columns: `format_schema` (`:65-71`) concatenates every column
of every table into one prompt string with no character budget. A miss does not negative-cache, so a
persistently failing `_load` re-runs on every request.

**Concurrency** — Entirely synchronous; called from inside the async SQL engine (`engines/sql.py:189`),
so the DuckDB connect + `information_schema` scan **blocks the event loop**. `self._cache` is mutated
without a lock (`:24`) — two concurrent misses both open a connection and both write, last write wins
(wasteful, not incorrect). **`get()` returns the cached dict by reference** (`:22`, `:25`) with no copy:
a caller that mutates it corrupts the cache for every later request.

**Complexity hotspots** — None. Longest function `_load` `:33-62` (30 LOC).

**Findings** — None of the catalogued IDs. Three unassigned observations: `invalidate()` (`:27-31`) is
dead — `rg -n 'invalidate' orchestrator/` matches only its definition, so nothing invalidates after a
sync-worker run and the model can be prompted with a schema up to 300 s stale; the DuckDB lockdown
config at `:42-47` is duplicated verbatim from `engines/sql.py:124-132` (the comment at `:37-39` says
so); and `engines/live_sf.py:56` calls `schema_cache()` — invoking the *instance*, which defines no
`__call__` (`:11-62`), so it always raises `TypeError`, swallowed by `except Exception: return ""` at
`live_sf.py:57-58`. `_object_hint()` therefore always returns `""` and the SOQL prompt never receives
the org-objects context it was written to carry.

---

## sf_dictionary

**Purpose** — Maps user vocabulary ("interview status") to Salesforce API names
(`Interview_Status__c`) from an org export, and injects a compact per-question hint into the SQL/SOQL
prompts.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `DICTIONARY_PATH` | `os.environ.get("SF_DICTIONARY_PATH", "/data/sf_dictionary.json")` | `sf_dictionary.py:26-28` |
| `MAX_OBJECTS` / `MAX_FIELDS_PER_OBJECT` | `4` / `60` | `sf_dictionary.py:32-33` |
| `_WORD_RE` / `_STOP` / `_cache` | regex / 30-word set / module global | `:35` / `:37-42` / `:44` |
| `_tokens` / `build_from_rows` / `save` / `load` / `available` | | `:47` / `:51` / `:73` / `:80` / `:92` |
| `_score` / `relevant_objects` | `(question_tokens, obj) -> int` / `(question, limit=4)` | `:96` / `:111` |
| `hint_for` | `(question: str) -> str` | [`sf_dictionary.py:123`](../../orchestrator/app/core/sf_dictionary.py#L123) |
| `build_from_xlsx` / `build_from_csv` / `main` | CLI `python -m app.core.sf_dictionary` | `:151` / `:162` / `:171` |

**Control flow** — `hint_for(question)` → `relevant_objects` `:129` → `load()` `:112` (returns `_cache`
if set `:83-84`, else reads + `json.loads` `:86`, and on **any** exception permanently caches
`{"objects": {}}` `:87-88`) → `_tokens(question)` `:113` (empty ⇒ `[]` `:114-115`) → every object scored
`:116-118` (`_score` re-tokenises the object api+label `:103` and **every field's api and label**
`:105-107`; 6 points per object-name hit, 1 per field hit) → sorted `(-score, api)` `:119`, truncated
`:120`. `hint_for` renders ≤60 fields per object `:134-139`, appends `… +N more` `:140`, and wraps with
a fixed instruction paragraph `:142-148`.

CLI `main` (`:171-188`): argparse → `build_from_xlsx` for `.xlsx`/`.xlsm` else `build_from_csv` `:182`
→ `save(data, args.out)` `:184` → prints counts `:187`.

**State & side effects** — Filesystem write in `save`: `mkdir(parents=True)` + `write_text(json.dumps())`
`:74-75`, **non-atomic** (no temp-file + rename). Reads in `load` `:86`, `build_from_csv` `:165`,
`build_from_xlsx` `:155`. Global mutation of `_cache` at `:77`, `:86`, `:88`. **Env read at import
time**: `SF_DICTIONARY_PATH` `:26-27`, so it is not re-read and not routed through `config.py`. No
network, no DB, no GPU.

**Dependencies** — Inbound: `engines/sql.py:97`/`:99`; `engines/live_sf.py:62`/`:68`;
`tests/test_sf_dictionary.py:10`; operator command documented at `README.md:247`. Outbound: `json`,
`os`, `re`, `pathlib`, `typing`; lazily `openpyxl` `:153`, `csv` `:163`, `argparse` `:172`.
**`from ..config import settings` at `:24` is imported and never used.**

**Config** — `SF_DICTIONARY_PATH` (`sf_dictionary.py:26`) only. **Absent from `.env.example`.**

**Failure modes** — `load()` catches **bare `Exception`** at `:87` and permanently memoises the empty
dictionary: missing file, corrupt JSON, permission error and `MemoryError` are indistinguishable and
all silent — there is no log line anywhere in the module. Because `_cache` is only ever set once
(`:83-84`), **a dictionary written after the orchestrator process started — which is exactly the
documented workflow at `README.md:247`, run in a separate process — is never picked up.** `save` is
non-atomic, and an interrupted write leaves truncated JSON that `load` swallows. `build_from_rows`
(`:51-70`) indexes `row[3]`/`row[4]` guarded only by `len(row) > 2` at `:62` — a 3-column export raises
`IndexError` at `:66-67`. `build_from_xlsx` (`:151-159`) passes a caller-supplied path straight to
`openpyxl.load_workbook` with **no `archive.check_zip_container` guard**, unlike `profile.py:167`.
`main()` has no `try/except`. No bound on dictionary size anywhere.

**Concurrency** — Fully synchronous; `hint_for` is called from `async def _ask_sql`
(`engines/sql.py:99`) and `async def write_soql` (`engines/live_sf.py:68`), so it runs **on the event
loop**. `_cache` (`:44`) is unsynchronised (benign duplicate loads). `_score` (`:96-108`) is
O(objects × fields) *per question* and re-runs `_WORD_RE.findall` plus set construction on every field
api and label with no memoisation — for a 1 000-object × 200-field export that is ~400 000 regex scans
per request, synchronously.

**Complexity hotspots** — None over 60 LOC. Longest: `hint_for` `:123-148` (26 LOC),
`build_from_rows` `:51-70` (20 LOC).

**Findings** — `REL-03` (bare `except` at `:87` that permanently degrades the feature with no log).
Unassigned: the stale-`_cache` behaviour above makes the documented rebuild workflow ineffective
without a restart; `available()` (`:92-93`) has no production caller; the dead `settings` import at
`:24`; and `_STOP` (`:37-42`) includes `"name"`/`"names"`, so "what is the account name field?" loses
its most discriminating token.

---

## repo_index

**Purpose** — Split cloned repository source files into overlapping line-windows (`CodeChunk`) so the
repo Q&A engine can cite `path:Lstart-Lend`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `CHUNK_LINES` / `OVERLAP_LINES` | `60` / `10` | `repo_index.py:13-14` |
| `CodeChunk` | `@dataclass(path, start_line, end_line, text)` | `repo_index.py:17-22` |
| `chunk_file` | `(path: str, text: str) -> List[CodeChunk]` | `repo_index.py:25` |
| `index_repo` | `(repo_dir: str, max_chunks: int = 6000) -> List[CodeChunk]` | [`repo_index.py:46`](../../orchestrator/app/core/repo_index.py#L46) |

**Control flow** — `index_repo` iterates `repo.iter_source_files(repo_dir)` `:49`, calls
`repo.read_text(ap)` then `chunk_file(rel, text)` `:50`, and returns early once `len(out) >= max_chunks`
`:51-53`. `chunk_file` splits on `splitlines()` `:27`, returns `[]` for empty `:28-29`, then windows
`end = min(n, start+60)` `:34`, body `"\n".join(lines[start:end]).strip()` `:35`, appended only when
non-empty `:36-39`, advancing `start = max(end-10, start+1)` `:42` — the `start+1` guard prevents an
infinite loop when `CHUNK_LINES <= OVERLAP_LINES`.

**State & side effects** — Pure in-memory; the only I/O is delegated to `repo.read_text` `:50`. No DB,
no network, no env reads. The result is persisted by the caller at `engines/repo.py:44-51`
(`db.replace_repo_chunks`).

**Dependencies** — Inbound: `engines/repo.py:17` (import), `:42` `index_repo(dest)`; tests
`tests/test_repo.py:10,36,60`. Outbound: `dataclasses`, `typing`, `.repo.iter_source_files`,
`.repo.read_text` `:11`. `Iterable`/`Tuple` imported at `:9` and unused.

**Config** — None. `CHUNK_LINES`, `OVERLAP_LINES` and `max_chunks` are hard-coded (`:13`, `:14`, `:46`);
nothing is env-tunable.

**Failure modes** — Nothing raises here; `read_text` already swallows `OSError` (`repo.py:239-243`).
There is **no character bound** in `chunk_file` — chunking is by line count only (`:34-35`), so a file whose
content is one very long line (minified JS/CSS, single-line JSON) up to `_MAX_FILE_BYTES = 400_000`
(`repo.py:47`) produces one chunk of up to ~400 000 characters. `index_repo` caps chunk *count* at 6000
but not total bytes. No timeout, no cancellation checkpoint in the loop `:49-53`.

**Concurrency** — Fully synchronous, no module-level mutable state. Called from `async def
_clone_and_index` (`engines/repo.py:42`), so it reads and chunks up to `settings.repo_max_files` =
20 000 files **on the event loop**.

**Complexity hotspots** — None. `chunk_file` `:25-43` is 19 LOC.

**Findings** — None of the catalogued IDs. Unassigned: the `max_chunks` early return (`:52-53`)
truncates mid-file with no marker in the returned data and no signal to the caller, and
`engines/repo.py:43-51` stores the truncated set as if complete — silently incomplete code search.

---

## repo

**Purpose** — Detect a GitHub URL, shallow-clone it into a per-conversation workspace under quota/TTL,
and build a language/tree/README overview. Cloned code is treated as data and never executed.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_REPO_RE` | GitHub repo/tree/blob URL regex | `repo.py:23-27` |
| `_SKIP_DIRS` / `_TEXT_EXT` / `_LANG` / `_MAX_FILE_BYTES` | 14 / 40 entries; ext→lang; `400_000` | `repo.py:30-47` |
| `RepoError` | `class(RuntimeError)` | `repo.py:50` |
| `GithubRef` | `@dataclass(owner, repo, ref=None, path=None)`; `.key`, `.clone_url` | `repo.py:54-67` |
| `detect_github` | `(text: str) -> Optional[GithubRef]` | `repo.py:70` |
| `_dir_size_bytes` / `enforce_quota_and_ttl` | `(path) -> int` / `() -> None` | `repo.py:83` / `:94` |
| `workspace_path` | `(conversation_id, ref) -> str` | `repo.py:122` |
| `_github_repo_size_kb` | `(ref) -> Optional[int]` | `repo.py:130` |
| `shallow_clone` | `(ref, dest) -> str` (commit SHA) | [`repo.py:151`](../../orchestrator/app/core/repo.py#L151) |
| `iter_source_files` / `read_text` | generator `(rel, abs)` / `(path) -> str` | `repo.py:221` / `:238` |
| `RepoOverview` / `build_overview` | `@dataclass` / `(repo_dir, max_tree_entries=200)` | `repo.py:256-264` / `:266` |

**Control flow** — driven from `engines/repo.py:28-52`:

1. `detect_github(text)` at `main.py:464-466` → `_REPO_RE.search` `repo.py:72`; `path` kept only for
   `/blob/` URLs `:76-77`.
2. `enforce_quota_and_ttl()` `:94-119`: returns if `settings.workspace_dir` is absent `:98-99`;
   `import time` **inside the function** `:100`; `shutil.rmtree` anything older than
   `workspace_ttl_hours * 3600` `:109-110`; `total = sum(_dir_size_bytes(p) …)` over survivors `:114`;
   then evicts oldest-first, calling `_dir_size_bytes(p)` **a second time** per victim `:115-119`.
3. `workspace_path` `:122-124`: `re.sub(r"[^A-Za-z0-9_.-]", "_", f"{conversation_id}__{owner}__{repo}")`
   joined onto `settings.workspace_dir`.
4. `shallow_clone` `:151-215`: `_github_repo_size_kb` does a **synchronous** `httpx.get` to
   `https://api.github.com/repos/{owner}/{repo}` with `timeout=10.0` `:136-140` (404 → `RepoError`
   `:141-142`; `httpx.HTTPError`/`ValueError`/`KeyError` → `None` `:147-148`); reject if
   `size_kb > repo_max_mb * 1024` `:155-159`; `rmtree` + `makedirs` `:161-163`; env hardening
   `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/true`, `GIT_CONFIG_NOSYSTEM=1` `:165-172`;
   `git -c core.hooksPath=/dev/null -c credential.helper= clone --depth 1 --no-tags --single-branch`
   via `subprocess.run(check=True, capture_output=True, timeout=180)` `:173-184`; `rmtree(dest/.git/hooks)`
   `:194`; post-clone file-count cap via full `os.walk` `:197-203` and on-disk byte cap `:204-206`;
   `git -C dest rev-parse HEAD` with `timeout=15`, any `SubprocessError` → `sha = ""` `:208-215`.
5. `build_overview` `:266-302`: one pass over `iter_source_files` `:272` counting languages `:275-277`,
   ≤200 tree entries `:278-279`, entry points `:280-282`, configs `:283-284`; probes four README names
   and reads the first 8000 chars `:286-291`.

**State & side effects** — **Network egress**: `https://api.github.com/repos/{owner}/{repo}` (`:137`)
and `git clone https://github.com/{owner}/{repo}.git` (`:67`, `:180`). Both hosts are hard-coded,
unauthenticated, and **do not go through the SSRF-guarded `core/net.py`** — this is the only outbound
internet dependency in the package and it contradicts the "fully local" framing. Filesystem
writes/deletes: `shutil.rmtree` at `:110,119,162,186,189,194,199,205`; `os.makedirs` `:163`; the clone
itself. Process spawn: `subprocess.run(["git", …])` `:182`, `:209`. **Env read: `dict(os.environ)`
copied and mutated for the child `:165-172` — the entire parent environment, including
`SF_CLIENT_SECRET` and `HF_TOKEN`, is inherited by `git`.**

**Dependencies** — Inbound: `engines/repo.py:15-16`, `:32`, `:33`, `:35`, `:41`; `core/repo_index.py:11`;
`main.py:464-466` (`detect_github`); `uploads.py:86-88` (`enforce_quota_and_ttl`). Tests
`tests/test_repo.py:9,14-33,54,68,92`. Outbound: `os`, `re`, `shutil`, `subprocess`, `dataclasses`,
`typing`, `..config.settings`; lazily `httpx` `:133` and `time` `:100`.

**Config** — `WORKSPACE_DIR` (default `/data/workspaces`, `config.py:214`) at `:95`/`:124` — **not in
`.env.example`**; `WORKSPACE_TTL_HOURS` (24, `config.py:215`) at `:109`; `WORKSPACE_QUOTA_GB` (20,
`config.py:216`) at `:113`; `REPO_MAX_MB` (300, `config.py:212`) at `:155,158,204,206`; `REPO_MAX_FILES`
(20000, `config.py:213`) at `:198,202`. Direct `os.environ` copy at `:165`.

**Failure modes** — Raises `RepoError` at `:142,156,187,191,200,206`. Swallowed: `OSError` on `getsize`
`:89-90` and `:233-234`; `httpx.HTTPError`/`ValueError`/`KeyError` in `_github_repo_size_kb` `:147-148`
→ returns `None` and **the clone proceeds with no pre-flight size check at all** (and `api.github.com`
is 60 req/h unauthenticated per IP, so this is the steady state under load);
`subprocess.SubprocessError` on `rev-parse` `:213-214` → the commit SHA is silently `""` and persisted
by `db.save_repo`, destroying provenance. `shutil.rmtree(..., ignore_errors=True)` at eight sites means
a failed deletion is invisible and quota enforcement can silently no-op forever. **All size caps are
post-hoc**: the GitHub `size` field is the *packed* size, and the on-disk check at `:204` runs only
after the clone finished writing — the 180 s subprocess timeout is the only real bound on bytes
written. `enforce_quota_and_ttl` has no error handling; `uploads.py:87-90` wraps it in
`try/except Exception: pass` but `engines/repo.py:32` does **not**. `subprocess.run(capture_output=True)`
buffers all of git's output in memory with no cap. `workspace_path` does not bound filename length — a
long `conversation_id` yields `OSError: File name too long` at `:163`.

**Concurrency** — Fully synchronous module, entirely called from `async def _clone_and_index`
(`engines/repo.py:28`): **the event loop is blocked for up to 180 s (clone) plus 10 s (GitHub API) plus
two full `os.walk` size scans plus a 20 000-file overview pass.** Every other in-flight chat stream
stalls. `uploads.py:88` has the same problem on every upload. The workspace directory is shared global
state with **no locking**: two concurrent requests can `enforce_quota_and_ttl` simultaneously and evict
a workspace another request is mid-clone or mid-read, and `shutil.rmtree(dest)` at `:162` will delete a
directory another conversation is reading. `_dir_size_bytes` is invoked twice per evicted entry
(`:114`, `:118`), doubling an already O(files-on-disk) `stat` storm under a 20 GB quota.

**Complexity hotspots** — `shallow_clone` [`repo.py:151-215`](../../orchestrator/app/core/repo.py#L151)
= **65 LOC, ~12 branches**. `build_overview` `:266-302` = 37 LOC, 7 branches.
`enforce_quota_and_ttl` `:94-119` = 26 LOC with two nested O(n) walks.

**Findings** — `SEC-05` (cloned third-party source is read into the prompt at `engines/repo.py:64-74`
and `:80-89` with no instruction-stripping or provenance tainting), `REL-03` (three swallowed
exception classes plus eight `ignore_errors=True` deletions, none logged), `DATA-03` (`repos` and
`repo_chunks` declare no foreign key, so `db.delete_conversation` at `db.py:334-340` orphans the rows
while the workspace directory is separately reclaimed only by TTL/quota). Unassigned: `ref.ref` (group
3 of `_REPO_RE`, `[^/\s]+`) is passed as the value of `--branch` at `:179` with no validation.

---

## chart_spec

**Purpose** — Pydantic model for a renderer-independent chart description, plus the parser that turns
raw LLM text into a validated spec or `None`. Owns the SSE wire shape.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `ChartType` | `Literal[bar, line, scatter, pie, area, horizontal_bar, donut, funnel, histogram]` | `chart_spec.py:40-52` |
| `CHART_TYPES` | same 9 as a tuple | `chart_spec.py:56-66` |
| `PART_TO_WHOLE_TYPES` | `frozenset({"pie","donut"})` | `chart_spec.py:70` |
| `MIN_BINS` / `MAX_BINS` | `2` / `50` | `chart_spec.py:74-75` |
| `ChartSpec` | `BaseModel`, `ConfigDict(extra="forbid", populate_by_name=True)` | `chart_spec.py:83`, `:95` |
| `ChartSpec.wire_dump` | `() -> Dict[str, Any]` | `chart_spec.py:154-175` |
| `parse_chart_spec` | `(raw: object, columns: Optional[Sequence[str]] = None) -> Optional[ChartSpec]` | [`chart_spec.py:189`](../../orchestrator/app/core/chart_spec.py#L189) |
| `_extract_json` | `(text) -> Optional[str]` | `chart_spec.py:178-186` |

Fields: `type` `:97`; `x_key` with `AliasChoices("x_key","x")` `:98`; `y_keys: List[str]` with
`AliasChoices("y_keys","y")` `:99`; `title=""` `:100`; `stacked=False` `:101`;
`bins: Optional[int] = Field(default=None, ge=MIN_BINS, le=MAX_BINS)` `:107`; `show_legend=True` `:108`;
`show_values=False` `:109`. Validators at `:111-150`.

**Control flow** — `parse_chart_spec`:

1. Already a `ChartSpec` ⇒ skip parsing `:198-199`.
2. `str`/`bytes` ⇒ `_extract_json` strips `<think>…</think>` `:179`, unwraps a ```` ```json ```` fence
   `:180-182`, takes the outermost `{ … }` by `find("{")`/`rfind("}")` `:183-186`; `json.loads` failure
   ⇒ `None` `:201-208`.
3. Non-dict payload ⇒ `None` `:209-210`.
4. `ChartSpec.model_validate(payload)`; `ValidationError` ⇒ `None` `:211-214`.
5. **Column binding — the trust boundary for model output**: if `columns` is given, `x_key` and every
   `y_keys` entry must be in `set(columns)`, else `None` `:216-219`.

`_normalize_options` (`:138-150`) mutates via `object.__setattr__`: drops `bins` on non-histograms
`:143-144`, truncates `y_keys` to one element for pie/donut `:148-149`.

**State & side effects** — None. No I/O, no DB, no network, no env reads, no global mutation.

**Dependencies** — Inbound: `chart_data.py:17` (`MIN_BINS`/`MAX_BINS`), `chart_decision.py:31`,
`chart_pipeline.py:27`, `charts_png.py:23`; tests `test_chart_spec.py:5`, `test_chart_routes.py:312`,
`test_charts_png.py:11`, `test_imports.py:8`. Outbound: `json`, `re`, `typing`, `pydantic`.

**Config** — None.

**Failure modes** — `parse_chart_spec` swallows `json.JSONDecodeError`/`ValueError` (`:207`) and
`ValidationError` (`:213`) and returns `None` **silently, with no log line** — a model that keeps
emitting malformed specs is invisible. Direct construction (`chart_pipeline.py:147`,
`chart_decision.py:615`) bypasses `parse_chart_spec` and therefore raises `ValidationError` to the
caller. `_extract_json`'s `rfind("}")` will span two adjacent JSON objects in one reply; the result then
fails `json.loads` → `None`. No input-length bound; `_FENCE_RE`/`_THINK_RE` are non-greedy and linear
(no ReDoS).

**Concurrency** — Fully synchronous; all module constants are immutable tuples/frozensets/compiled
regexes, so no shared mutable state.

**Complexity hotspots** — None. Largest function is `parse_chart_spec` at 32 LOC (`:189-220`).

**Findings** — None of the catalogued IDs. Unassigned: `_LEGACY_WIRE_KEYS` (`:77`) is dead (`rg` matches
only its definition), and the `bins` bound `[2,50]` (`:107`) conflicts with `chart_data`'s legitimate
`bin_count == 1` return (`chart_data.py:92-94`) — `chart_pipeline.py:147-154` constructs a `ChartSpec`
with that value and the resulting `ValidationError` is eaten by the blanket handler at
`chart_pipeline.py:115`, so a constant-valued histogram silently produces no chart.

---

## chart_data

**Purpose** — Deterministic, trusted histogram binning over already-returned rows, so the browser
(ECharts) and the report (matplotlib) draw identical bars. The model never chooses bin edges.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `BIN_COLUMN` / `COUNT_COLUMN` | `"bin"` / `"count"` | `chart_data.py:21-22` |
| `_DEFAULT_MIN_BINS` / `_DEFAULT_MAX_BINS` | `5` / `20` | `chart_data.py:27-28` |
| `default_bin_count` | `(n: int) -> int` — `ceil(sqrt(n))` clamped `[5,20]`, `1` when `n<=1` | `chart_data.py:31-36` |
| `clamp_bins` | `(bins: Optional[int], n: int) -> int` — clamps to `[2,50]` | `chart_data.py:39-47` |
| `_fmt_edge` | `(v: float, integral: bool) -> str` | `chart_data.py:50-55` |
| `build_histogram` | `(columns, rows, value_column, bins=None) -> Optional[Tuple[List[str], List[List[object]], int]]` | [`chart_data.py:58`](../../orchestrator/app/core/chart_data.py#L58) |

**Control flow** — `build_histogram`: locate `value_column`, missing ⇒ `None` `:72-75`; collect finite
numeric values via `chart_profile._as_number` + `math.isfinite`, per-row `IndexError/KeyError/TypeError`
swallowed with `continue` `:77-86`; no values ⇒ `None` `:86-87`; `lo, hi = min, max` and
`integral = all(float(v).is_integer() …)` `:89-90`; degenerate `lo == hi` ⇒ one row, **bin_count = 1**
`:92-94`; else `k = clamp_bins(bins, len(values))`, `width = (hi-lo)/k` `:96-98`; assign each value to
`slot = int((v-lo)/width)` clamped to `k-1` `:99-104`; emit `[f"{lo_edge} - {hi_edge}", count]` with the
last bin's upper edge exactly `hi` `:106-113`.

**State & side effects** — None. Pure.

**Dependencies** — Inbound: `chart_pipeline.py:24`/`:143`; `tests/test_chart_data.py:8-15`. Outbound:
`math`; **`chart_profile._as_number` (`:16`) — a private symbol imported across modules**;
`chart_spec.MIN_BINS/MAX_BINS` (`:17`).

**Config** — None.

**Failure modes** — `width = (hi-lo)/k` at `:97` divides by zero if `hi-lo` underflows to `0.0` while
`lo != hi` (subnormal inputs only); the `ZeroDivisionError` propagates to `chart_pipeline`'s blanket
`except` at `chart_pipeline.py:115`. `hi-lo` can overflow to `inf` near ±1.5e308: `width = inf`, every
value lands in bin 0, the last label reads `"… - inf"` — no crash, silently wrong chart. Row-shape
errors swallowed at `:81-82` with no counter or log. **The `bins` parameter is never supplied by any
caller** (`chart_pipeline.py:143` omits it), so `clamp_bins`'s non-default branch is unreachable in
production.

**Concurrency** — Synchronous, pure, no shared state.

**Complexity hotspots** — `build_histogram` `chart_data.py:58` = 56 LOC, straight-line, cyclomatic ≈ 9.
Under both thresholds.

**Findings** — None of the catalogued IDs. Unassigned, verified by execution in the evidence pass:
`_fmt_edge` with `integral=True` (`:51-52`) rounds edges to `int`, so when `k` exceeds the integer range
the labels collapse — `build_histogram(['amount'], [[i%2+1] for i in range(100)], 'amount')` returns
four bins labelled `"1 - 1"` and five labelled `"2 - 2"`.

---

## chart_decision

**Purpose** — The trusted, deterministic engine that decides *whether* and *how* to chart a result set,
from user wording plus column metadata. The only module permitted to say "ask the model".

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| Trigger regexes | `LEGACY_CHART_RE` `:40-42`, `_NATURAL_RE` `:45-52`, `_NAMED_CHART_RE` `:53-57`, `_BARE_NAMED_RE` `:58`, `_FALSE_POSITIVE_RE` `:62-70`, `_MODIFIER_RE` `:80-89`, `_SUPPRESS_RE` `:94-104`, `_TYPE_PHRASES` `:132-151`, `_STACK_RE` `:163` | `chart_decision.py` |
| `chart_suppressed` / `explicit_chart_request` | `(message) -> bool` | `:107-109` / `:112-127` |
| `requested_chart_type` / `requested_stacked` | `(message) -> Optional[str]` / `-> bool` | `:154-160` / `:166-167` |
| `STANDARD_STAGE_ORDERS` | `opportunity` (10), `lead` (4), `case` (4) | `:184-207` |
| `_load_custom_orders` / `stage_orders` / `trusted_stage_order` | | `:210-227` / `:230-234` / `:237-252` |
| `can_funnel` | `(profile, labels) -> bool` | `:255-256` |
| Caps | `VERTICAL_BAR_MAX_CATEGORIES=8`, `LONG_LABEL_CHARS=16`, `MAX_CATEGORIES=40`, `MAX_PART_TO_WHOLE_CATEGORIES=6` | `:265-271` |
| `ChartDecision` | `@dataclass(should_chart, chart_type, reason, confidence, x_key, y_keys, stacked, use_model, histogram_source)`; `as_dict()` | `:274-298` |
| `decide` | `(message, columns, rows, mode="explicit", profiles=None, explicit_override=None) -> ChartDecision` | [`:341`](../../orchestrator/app/core/chart_decision.py#L341) |
| `_decide_explicit` / `_unambiguous_shape` / `_decide_hybrid` | | `:390` / `:481` / `:526` |
| `build_spec` | `(decision, columns, title="") -> Optional[ChartSpec]` | `:599-623` |

**Control flow** — `decide`: materialise columns/rows `:361-362`; `explicit = explicit_chart_request(...)`
unless overridden `:363-367`; empty ⇒ `"empty_result"` `:369-370`; **`chart_suppressed(message)` outranks
`explicit_override`** `:375-376`; profile columns `:378`; split into numeric / dims (categorical+boolean)
/ dates `:379-381`; explicit ⇒ `_decide_explicit` `:383-384`; `mode == "hybrid"` ⇒ `_decide_hybrid`
`:385-386`; any other mode ⇒ `"not_requested"` `:387`.

`_decide_explicit` `:390-479`: histogram needs ≥1 numeric and takes **`histogram_source = numeric[0].name`
— the first numeric column in result order, not the column the user named** `:403-414`; funnel needs a dim
plus a numeric, downgrading to `horizontal_bar` at confidence 0.6 when `trusted_stage_order` is unknown
`:416-434`; scatter uses `numeric[0]`/`numeric[1]` by column order `:436-443`; pie/donut needs dim +
numeric, downgrading on a negative metric `:445-458`, **with no category-count cap**;
bar/horizontal_bar/line/area set `y_keys = [n.name for n in numeric]` — **every numeric column** `:468`,
again with no cap; an unnamed "chart" falls to `_unambiguous_shape` `:472-475`; otherwise
`use_model=True` `:478`.

`build_spec` `:599-623`: rejects when not charting / no type / no `x_key` `:606`; `x_key` must be a real
column `:608-610`; `y_keys` filtered to real columns and rejected if empty `:611-613`; constructs the
`ChartSpec` inside a **bare `except Exception: return None`** `:622-623`.

**State & side effects** — One env read, `os.getenv("CHART_FUNNEL_STAGE_ORDER")` at `:211`, **re-read on
every call** to `stage_orders()` → `trusted_stage_order()`. No DB, filesystem, network or GPU.

**Dependencies** — Inbound: `chart_pipeline.py:25` and `:226` (function-local import of
`trusted_stage_order`), `engines/sql.py:20`/`:33`; tests `test_chart_decision.py:9,395`. Outbound:
`json`, `os`, `re`, `dataclasses`, `typing`; `chart_profile.ColumnProfile/profile_columns` `:30`;
`chart_spec.ChartSpec` `:31`.

**Config** — `CHART_FUNNEL_STAGE_ORDER` read directly from `os.getenv` at `:211`, **not** through
`config.py` (unlike `CHART_TRIGGER_MODE`, validated at `config.py:230-231` against
`("explicit","hybrid")`). Declared at `.env.example:77-78` — but **neither variable is forwarded to the
container by `docker-compose.yml:219-267`**, so both are inert in the deployed system.

**Failure modes** — `trusted_stage_order` `:251` raises **`KeyError`** for blank/whitespace labels:
`distinct` excludes them at `:245` but the `sorted(seen, …)` key at `:251` indexes every element of
`seen` — reproduced in the evidence pass as `trusted_stage_order(['Prospecting','Closed Won',''])` →
`KeyError('')`. `_load_custom_orders` swallows `json.JSONDecodeError`/`ValueError` at `:216-217` and
returns `{}` **with no log**, so a typo'd `CHART_FUNNEL_STAGE_ORDER` silently disables the operator's
custom order. `build_spec` `:622` hides every `ChartSpec` validation error. `_labels_of` `:336-337`
swallows per-row indexing errors. No I/O, so no timeouts apply.

**Concurrency** — Fully synchronous. `stage_orders()` rebuilds a dict from the environment on every
call — no caching, no lock, no shared mutable state.

**Complexity hotspots** —
`_decide_explicit` [`chart_decision.py:390`](../../orchestrator/app/core/chart_decision.py#L390) =
**91 LOC, ~14 decision points** (the largest function in the whole chart pipeline);
`_decide_hybrid` `:526` = **73 LOC, ~13 decision points**; `decide` `:341` = 49 LOC;
`_unambiguous_shape` `:481` = 43 LOC.

**Findings** — `REL-03` (bare `except Exception: return None` at `:622` plus the silent
`_load_custom_orders` degradation at `:216-217`). Unassigned, all verified by execution in the evidence
pass: `_MODIFIER_RE`'s `an?\s+\w+` alternative (`:82`) matches *any* "make it a &lt;word&gt;", so
`"make it a table"`, `"make it a csv"` and `"make it an export"` all return `True` from
`explicit_chart_request` and none is caught by `_SUPPRESS_RE` — `decide("make it a table", ['stage','total'], …)`
returns `should_chart=True, chart_type='funnel'`; the explicit named-type path applies no
category-count cap (a 300-row pie is accepted) and puts every numeric column on one linear axis; and
four symbols are dead — `can_funnel` `:255-256`, `ChartDecision.as_dict` `:292-298`, `_NO` `:301`, and
`engines/sql.py:33`'s `CHART_RE` re-export.

---

## chart_pipeline

**Purpose** — The single entry point shared by the SQL engine, the agent route and the report engine:
decide → optionally ask the model → validate → prepare data. Guarantees it never raises.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `log` | `logging.getLogger(__name__)` | `chart_pipeline.py:29` |
| `AskModel` | `Callable[[List[dict]], Awaitable[str]]` | `chart_pipeline.py:32` |
| `ChartResult` | `@dataclass(spec, columns, rows, reason="", confidence=0.0, derived=False)` | `chart_pipeline.py:35-46` |
| `chart_prompt` | `(question, profiles, types) -> List[dict]` | `chart_pipeline.py:49-76` |
| `MODEL_CHART_TYPES` | 7 types; `histogram` and `funnel` deliberately excluded (`:79-82`) | `chart_pipeline.py:83-91` |
| `build_chart` | `async (message, columns, rows, *, mode="explicit", ask_model=None, title="", force=False) -> Optional[ChartResult]` | [`chart_pipeline.py:94`](../../orchestrator/app/core/chart_pipeline.py#L94) |
| `_build_chart` / `_auto_title` / `_repair` / `_order_rows` | | `:120` / `:189` / `:194` / `:214` |

**Control flow** — `build_chart` delegates to `_build_chart` inside a `try` `:110-114`; a **blanket
`except Exception`** logs a warning and returns `None` `:115-117`. `_build_chart`: empty columns/rows ⇒
`None` `:130-131`; `profile_columns` `:133`; `decide(..., explicit_override=True if force else None)`
`:134-137`; not charting ⇒ `None` `:138-139`. Then one of three branches:

- **histogram** `:142-158`: `build_histogram(columns, rows, decision.histogram_source)` `:143` (no `bins`
  argument); `None` ⇒ `None`; construct `ChartSpec(type="histogram", x_key="bin", y_keys=["count"],
  bins=k, show_legend=False)` `:147-154`; return `ChartResult(derived=True)` `:155-158`.
- **model** `:161-173`: `ask_model is None` ⇒ `None`;
  `await ask_model(chart_prompt(message, profiles, MODEL_CHART_TYPES))` `:164`;
  `parse_chart_spec(raw, columns=columns)` `:165`; `_repair` `:168`; return `ChartResult(..., "model_spec", 0.5)`.
- **deterministic** `:176-186`: `build_spec(decision, columns, title=title or _auto_title(decision))`;
  `_order_rows` `:179`; `derived = ([list(r) for r in rows] != ordered)` `:183`.

`_repair` `:194-211` keeps only `y_keys` whose profile `is_numeric` `:202`, requires a numeric `x_key`
for scatter `:205-207`, and returns `spec.model_copy(update={"y_keys": numeric})` `:211` — **`model_copy`
bypasses validators**, including `_normalize_options`. `_order_rows` `:214-247` uses function-local
imports of `trusted_stage_order` `:226` and `profile_column, _column_values` `:227` (the latter a private
symbol of another module) to avoid a circular import; funnels are sorted by trusted stage rank `:235-241`;
non-monotonic date axes for `line`/`area` are sorted by `str(r[xi])` — **lexicographically** `:243-246`.

**State & side effects** — **GPU egress**: `await ask_model(...)` `:164`; in production that is
`engines/sql.py:210-211 _ask_chart_model` → `llm.chat_completion(temperature=0.0, max_tokens=2500)`
against the local vLLM server, with **no timeout set at this layer**. Logging: `log.warning` `:116`. No
filesystem, DB, env reads or module-level mutable state.

**Dependencies** — Inbound: `engines/sql.py:21` → `attach_chart` `sql.py:214-243`, called at `sql.py:390`
(live SOQL) and `sql.py:426` (warehouse); `engines/agent.py:255`/`:269` (imports `attach_chart` from
`.sql`); `engines/report.py:22`/`:167`. Tests `test_chart_pipeline.py:8`, `test_report_charts.py:92`.
Outbound: `chart_data.build_histogram` `:24`; `chart_decision.{ChartDecision, build_spec, decide}` `:25`;
`chart_profile.{ColumnProfile, profile_columns}` `:26`; `chart_spec.{ChartSpec, parse_chart_spec}` `:27`.

**Config** — None read directly. `mode` comes from `settings.chart_trigger_mode` at `sql.py:234` and
`report.py:171`.

**Failure modes** — The blanket `except Exception` at `:115` converts **every** internal defect into a
silent "no chart" plus one WARNING line — it currently masks at least the `ChartSpec.bins`/`bin_count==1`
conflict and the `trusted_stage_order` `KeyError`. `ask_model` has **no timeout, no retry and no
cancellation guard at this layer**; a hung vLLM call stalls `attach_chart` and therefore the whole
`/chat` SSE stream, because `sql.py:426` is awaited before the narrative stream starts.
`parse_chart_spec` returning `None` is indistinguishable from "model unreachable" — both produce `None`
at `:166-167` with no log.

**Concurrency** — `async def`, but the only `await` is `ask_model` `:164`. Everything else — profiling,
deciding, binning, row materialisation, sorting — is CPU-bound work executed inline on the event loop,
over up to 500 preview rows for the SQL path (`sql.py:397,426`) or `rows[:50]` for reports
(`report.py:169`). No shared mutable state.

**Complexity hotspots** — `_build_chart`
[`chart_pipeline.py:120`](../../orchestrator/app/core/chart_pipeline.py#L120) = **69 LOC**, three
mutually exclusive branches, cyclomatic ≈ 12.

**Findings** — `REL-03` (the blanket handler at `:115` is the single largest silent-degradation surface
in the chart stack). Unassigned: `chart_prompt` (`:49-76`) is a genuinely tight prompt-injection
boundary — only `p.to_prompt_dict()` aggregate metadata is serialised at `:59` and **no cell value ever
reaches the model**; the user's `question` is the only free text `:75` (for reports that "question" is
`sec["instruction"]` from `report.py:168`, itself LLM-generated planner output). `derived` carries two
different meanings — "re-binned" `:157` and "re-ordered" `:185`. `:183` materialises a full second copy
of every row purely to compute a boolean.

---

## chart_profile

**Purpose** — Infer a per-column "shape" (kind, cardinality, range, label length) from returned rows,
emitting **aggregate metadata only** so no Salesforce cell value can reach a model prompt.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `ColumnKind` | `= str` | `chart_profile.py:26` |
| Regexes | `_BOOL_TOKENS` `:31`, `_SF_ID_RE` `:36`, `_ID_NAME_RE` `:37`, `_DATE_RE` `:39-43`, `_PERIOD_RE` `:45`, `_TIME_NAME_RE` `:47-50`, `_STAGE_NAME_RE` `:54` | |
| `_is_missing` / `_as_number` / `_is_datelike` / `_is_boolish` | | `:57` / `:61` / `:83` / `:92` |
| `ColumnProfile` | `@dataclass(name, kind, total, non_null, unique, minimum, maximum, has_negative, max_label_len, all_distinct, monotonic, time_named, stage_named)` | `:98-151` |
| `ColumnProfile.to_prompt_dict` | `() -> dict` | `:136-151` |
| `_column_values` | `(rows, index) -> List[object]` | `:154-161` |
| `profile_column` | `(name, values) -> ColumnProfile` | [`chart_profile.py:164`](../../orchestrator/app/core/chart_profile.py#L164) |
| `profile_columns` / `profile_index` | `(columns, rows) -> List[ColumnProfile]` / `(profiles) -> dict` | `:219` / `:229` |

**Control flow** — `profile_column` falls through in strict order: build the shell (`total`, `present`,
`non_null`, `distinct = {str(v) …}`, `max_label_len`, `all_distinct`, `time_named`, `stage_named`)
`:165-179`; no present values ⇒ `"categorical"` `:180-182`; all bool-ish ⇒ `"boolean"` `:184-186`; name
matches `_ID_NAME_RE` **and** every value matches `_SF_ID_RE` ⇒ `"identifier"` `:190-193`; all date-like ⇒
`"date"` with `monotonic = (labels == sorted(labels))` over the compacted present list `:195-199`; all
numeric ⇒ `"identifier"` if id-named else `"numeric"`, recording `minimum`/`maximum`/`has_negative`
`:201-208`; `all_distinct and max_label_len > 40` ⇒ `"text"`, else `"categorical"` `:212-215`.

**State & side effects** — None. Pure, stdlib only.

**Dependencies** — Inbound: `chart_data.py:16` (`_as_number`, private), `chart_decision.py:30`,
`chart_pipeline.py:26` and `:227` (`profile_column`, `_column_values` — private); tests
`test_chart_decision.py:19`, `test_chart_pipeline.py:9`. Outbound: `datetime`, `re`, `dataclasses`,
`decimal`, `typing`.

**Config** — None.

**Failure modes** — Nothing raises. `_column_values` `:158-160` swallows `IndexError/KeyError/TypeError`
and substitutes `None`, so a short row silently becomes a null. `_as_number` swallows
`InvalidOperation/ValueError` `:69-70`, `:77-79`. `profile_column` calls `str(v)` three separate times
per value (`:168`, `:175`, `:197`) — for a 500-row × 20-column preview that is 30 000 `str()` calls per
`attach_chart`, on the event loop.

**Concurrency** — Synchronous and pure; module-level regexes are immutable. Safe to call from anywhere.

**Complexity hotspots** — `profile_column`
[`chart_profile.py:164`](../../orchestrator/app/core/chart_profile.py#L164) = **55 LOC, ~14 branches** —
under the LOC threshold, over the branch threshold.

**Findings** — None of the catalogued IDs. This module is the enforcement point for the "no cell values
in a prompt" invariant, and the invariant holds **by construction**: `to_prompt_dict` (`:136-151`) emits
only `name/kind/rows/non_null/distinct` plus `min/max/has_negative` for numeric or `max_label_len`
otherwise, and that is the single shape reaching `chart_prompt` (`chart_pipeline.py:59`). Unassigned:
`profile_index` (`:229-230`) is dead (`chart_pipeline._repair:201` builds the same dict inline), and
`monotonic` (`:198`) compares *string* forms over `present` (nulls removed) while `_order_rows` sorts the
full row list — the two disagree when the date column has nulls.

---

## charts_png

**Purpose** — Render an already-validated `ChartSpec` to a PNG with matplotlib/Agg for pandoc report
embedding. The only server-side renderer; the browser draws ECharts from the same spec.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `UnsupportedChartType` / `EmptyChartData` | `class(ValueError)` | `charts_png.py:26-27` / `:30-31` |
| `PNG_SUPPORTED` | `frozenset`, 8 types (`funnel` excluded) | `charts_png.py:41-43` |
| `PNG_TABLE_ONLY` | `frozenset({"funnel"})` | `charts_png.py:48` |
| `_MAX_PIE_SLICES` | `8` | `charts_png.py:50` |
| `_num` / `supports` | `(value) -> float` / `(chart_type) -> bool` | `:53-61` / `:64-66` |
| `render_chart_png` | `(spec, columns, rows, out_path) -> Path` | [`charts_png.py:69`](../../orchestrator/app/core/charts_png.py#L69) |
| `_draw_part_to_whole` / `_draw_horizontal_bar` / `_draw_cartesian` | | `:136` / `:156` / `:182` |
| `_UNDECIDED` | import-time guard, raises `RuntimeError` if a `ChartType` has no report policy | `charts_png.py:227-231` |

**Control flow** — `render_chart_png`: reject non-`ChartSpec` ⇒ `TypeError` `:83-84`; type not in
`PNG_SUPPORTED` ⇒ `UnsupportedChartType` `:85-88`; `x_key` and every `y_key` must be real columns ⇒
`EmptyChartData` `:90-95`; no rows ⇒ `EmptyChartData` `:96-98`; **lazy** `import matplotlib` +
`matplotlib.use("Agg", force=True)` + `import matplotlib.pyplot as plt` `:100-103`;
`xs = [str(r[xi]) for r in rows]` `:105-106`; `fig, ax = plt.subplots(figsize=(8, 4.5))` **outside the
`try`** `:108`; inside the `try`, dispatch to one of three helpers `:110-115`, title `:117-118`, axis
labels except pie/donut `:119-127`, `tight_layout()` `:128`, `savefig(out_path, dpi=144)` `:129-130`;
`finally: plt.close(fig)` `:131-132`.

**State & side effects** — Filesystem write: `fig.savefig(out_path, dpi=144)` `:130`. `out_path` is
`tmp_dir / f"chart-{index}.png"` inside a `tempfile.TemporaryDirectory` (`report.py:189`, `:234`) — **no
user-controlled path component, no traversal risk**, and the directory is removed by the `with` block.
Global mutation: `matplotlib.use("Agg", force=True)` `:102` mutates the process-wide backend on **every**
call, and `plt.subplots` `:108` registers the figure in pyplot's global `Gcf`. No network, DB, env or GPU.

**Dependencies** — Inbound: `engines/report.py:23`, called at `report.py:190`; tests
`test_charts_png.py:12`, `test_imports.py:9`. Outbound: `pathlib`, `typing`,
`chart_spec.{CHART_TYPES, ChartSpec}` `:23`; lazily `matplotlib`, `matplotlib.pyplot` `:100-103`.

**Config** — None.

**Failure modes** — Raises `TypeError` `:84`, `UnsupportedChartType` `:86`, `EmptyChartData` `:92,95,98,147`.
All are caught by the blanket `except Exception` at `report.py:194-195`, so the section keeps its prose
and table and loses only the image. `_num` `:53-61` returns **`0.0`** for `None` and for anything
`float()` rejects — a NULL metric therefore draws as a real zero bar in the report while ECharts draws a
gap. **No figure leak**: `plt.close(fig)` is in a `finally` and `plt.subplots` precedes the `try`. No
bound on `len(rows)` and no timeout on the render.

**Concurrency** — **Synchronous**, called from `async def _section_chart` (`report.py:190`) with no
`to_thread`/executor — it blocks the event loop for its full duration. Measured on this machine
(`orchestrator/.venv`, aarch64): first-call `import matplotlib` + `pyplot` = **0.203 s**; a 50-row bar
chart (the report's slice size, `report.py:169`) = **0.113 s**; a 500-row bar chart = **0.700 s**.
`matplotlib.pyplot` is **not thread-safe** and this module uses the global pyplot API rather than the
object-oriented `Figure` + `FigureCanvasAgg`; there is no race today because everything runs on one
thread, but the natural fix for the blocking problem (`asyncio.to_thread(render_chart_png, …)`) would
introduce one.

**Complexity hotspots** — `render_chart_png`
[`charts_png.py:69`](../../orchestrator/app/core/charts_png.py#L69) = **67 LOC, cyclomatic ≈ 12**.
`_draw_cartesian` `:182` = 50 LOC with 7 type branches.

**Findings** — None of the catalogued IDs. Unassigned but material: **renderer divergence between the
report and the browser, all inside this file** — slices are re-sorted descending `:139` and truncated to
8 with an "Other" bucket `:140-143` (ECharts does neither); `scatter` plots against the **row index**
rather than the x value `:210`, so a scatter of `revenue` vs `headcount` is drawn as `headcount` vs
`0..n`, which is not a scatter plot; and NULL → `0.0` `:60-61`. `supports()` `:64-66` is dead in
production (`report.py:180` inlines the membership test). The import-time `_UNDECIDED` guard `:227-231`
is a good invariant: adding a `ChartType` without a report policy fails at import.

---

## exports

**Purpose** — Write a result set to `.xlsx` (openpyxl) or `.csv` in a given directory under a
`<slug>-<timestamp>.<ext>` filename, capped at 100 000 rows.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `PREVIEW_ROW_CAP` / `EXPORT_ROW_CAP` | `500` / `100_000` | `exports.py:15-16` |
| `_SLUG_RE` | `re.compile(r"[^a-z0-9]+")` | `exports.py:18` |
| `slugify` | `(text, max_len=40, fallback="export") -> str` | `exports.py:21-24` |
| `timestamped_filename` | `(slug, ext) -> str` | `exports.py:27-29` |
| `cap_rows` / `apply_export_cap` | `(rows, cap) -> Tuple[list, bool]` | `exports.py:32-38` / `:41-43` |
| `_cell_value` | `(value) -> object` | `exports.py:46-50` |
| `export_xlsx` | `(columns, rows, directory, slug, cap=EXPORT_ROW_CAP) -> Tuple[Path, bool]` | [`exports.py:53`](../../orchestrator/app/core/exports.py#L53) |
| `export_csv` | same signature | `exports.py:96` |

**Control flow** — `export_xlsx`: lazy `from openpyxl import Workbook, Font, get_column_letter` `:64-66`;
`apply_export_cap(rows, cap)` `:68`; `Path(directory).mkdir(parents=True, exist_ok=True)` and
`timestamped_filename` `:69-71`; `Workbook()` in memory (**not** `write_only`), sheet `"Data"`, bolded
header `:73-79`; append every data row through `_cell_value` `:80-81`; auto column widths from the header
plus the first 1000 rows, `min(longest + 2, 60)` `:84-90`; `wb.save(path)` `:92-93`. `export_csv`
`:104-113` mirrors the cap/mkdir/path logic then uses `csv.writer(newline="", encoding="utf-8")` with
`None` → `""` `:112`.

**State & side effects** — Filesystem writes: `mkdir` `:70`/`:106` and file creation `:92`/`:108`. The
production `directory` is `settings.reports_dir` (`engines/sql.py:409`), default `/reports`
(`config.py:100`, `docker-compose.yml:248`). No network, DB, env reads or globals.

**Dependencies** — Inbound: `config.py:11` (imports both caps as defaults, used at `config.py:234-235`);
`engines/sql.py:22`, used at `sql.py:397` (`cap_rows`) and `sql.py:407-409` (the exporters);
`engines/report.py:229` (`slugify`). Tests `test_exports.py:7`, `test_row_caps.py:2`, `test_imports.py:11`.
Outbound: `csv`, `re`, `time`, `pathlib`, `typing`; lazily `openpyxl` `:64-66`.

**Config** — None read here; the two module constants are the *defaults* that `config.py:234-235` reads
`SQL_PREVIEW_ROW_CAP` / `EXPORT_ROW_CAP` against.

**Failure modes** — `cap_rows` raises `ValueError` for a negative cap `:34-35`; `wb.save`/`open`
propagate `OSError` (disk full, permission) to `sql.py:408`, inside the `/chat` streaming worker.
`timestamped_filename` has **second granularity and no entropy** `:28`: two exports of the same question
in the same second silently overwrite one another. **No cap on total `REPORTS_DIR` size and no retention
or cleanup anywhere** — `rg -n 'retention|cleanup|prune|unlink|rmtree|max_age|purge' orchestrator/app/`
hits only `archive.py`, `repo.py` and `uploads.py`. `export_xlsx` builds the entire workbook in RAM:
measured at 100 000 rows × 10 string columns = **4.8 s wall, 433 MB peak RSS**, 3.5 MB output.

**Concurrency** — Both exporters are synchronous and called inline from `async def` code at
`engines/sql.py:408` — the measured 4.8 s is 4.8 s during which no other SSE stream, health check or
request progresses.

**Complexity hotspots** — None over 60 LOC. Largest is `export_xlsx` `:53` = 43 LOC.

**Findings** — `SEC-01` (every file written here lands in the flat directory that `main.py:257-271`
serves with no authentication; `main.py:55-56` states "/chat and /reports* remain auth-free").
Unassigned but confirmed by execution in the evidence pass: **values are written verbatim, so both
exporters are formula-injection sinks** — `export_xlsx(['Name','Amt'], [["=cmd|'/c calc.exe'!A0", 1]], …)`
produces a cell with `data_type == 'f'` (a live formula) and `export_csv` writes the line unescaped.
`slugify` `:22-23` strips everything outside `[a-z0-9]`, so path traversal via `slug` is **not** possible.
Two caller bugs: `sql.py:292` sizes the DuckDB fetch with the env-overridable `settings.export_row_cap`
but `sql.py:408-409` calls the exporter without `cap=`, so `EXPORT_ROW_CAP=250000` fetches 250 001 rows
and silently writes 100 000; and `sql.py:408` binds the truncation flag to `_export_truncated` and never
uses it, so `meta` (`sql.py:414-421`) never tells the user the export was cut.

---

## pdf

**Purpose** — Turn a base64 PDF into page PNG data-URLs plus its text layer, for the multimodal model.
Uses pypdfium2 (self-contained arm64 wheel).

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `MAX_PDF_PAGES` / `RENDER_SCALE` / `MAX_TEXT_CHARS` | `6` / `2.0` / `24000` | `pdf.py:18-20` |
| `_strip_data_url` | `(b64) -> str` | `pdf.py:23-24` |
| `render_pdf` | `(pdf_base64, max_pages=MAX_PDF_PAGES) -> Tuple[List[str], str, int]` | [`pdf.py:27`](../../orchestrator/app/core/pdf.py#L27) |

**Control flow** — lazy `import pypdfium2 as pdfium` `:35`;
`base64.b64decode(_strip_data_url(pdf_base64))` — **no `validate=True`, no size check** `:37`;
`pdfium.PdfDocument(pdf_bytes)` parses untrusted bytes in a C library `:38`;
`n = min(len(pdf), max_pages)` `:40-42`; per page — `get_textpage()` → `get_text_range()` → `close()`
`:46-48`, `page.render(scale=RENDER_SCALE)` `:50`, `bitmap.to_pil().convert("RGB")` `:51`,
`pil.save(buf, format="PNG")` `:52-53`, base64 data-URL appended `:54-57`, `page.close()` `:58`; join
per-page text with `--- Page N ---` headers and truncate to `MAX_TEXT_CHARS` `:60-64`; return
`(images, text, total)` `:65`; `finally: pdf.close()` `:66-67`.

**State & side effects** — Native memory allocation by PDFium for page bitmaps, Pillow copies, base64
strings. No filesystem, network, DB, env reads, globals or direct GPU use (the *result* is fed to vLLM by
`engines/document.py:66-71`).

**Dependencies** — Inbound: `engines/document.py:13`, called at `document.py:34` inside `async def
run_pdf_engine`, reached from `POST /chat` with a `pdf` field (`main.py:196-203`, `:551-557`);
`core/extract.py:58-61` (`_extract_pdf_text`, the web-fetch/URL ingestion path). Tests
`test_extract.py:53-55`. Outbound: `base64`, `io`, `typing`; lazily `pypdfium2` and transitively Pillow.

**Config** — None. All three constants are hard-coded with no env override.

**Failure modes** — `base64.b64decode` without `validate=True` `:37` silently drops non-alphabet
characters; bad padding raises `binascii.Error`, which propagates out of `run_pdf_engine`
(`document.py:34` has no `try`). Any `PdfiumError` from `:38` propagates the same way. **No bound on the
decoded PDF size and no bound on rendered pixel dimensions**: `page.render(scale=2.0)` `:50` allocates
`ceil(w_pt*2) × ceil(h_pt*2) × 4` bytes, and the PDF format permits a MediaBox up to 14 400 × 14 400 pt →
28 800 × 28 800 × 4 ≈ **3.3 GiB per page**, for up to 6 pages. `page.close()` `:58` is **not** in a
`finally`, so a raise in `render`/`to_pil`/`save` leaks the page handle; `bitmap` is never closed at all.
All rendered pages are held simultaneously in `images` as base64 strings (4/3 blowup) and then all
appended to one model message (`document.py:50-51`) — only `MAX_TEXT_CHARS` bounds the *text*, nothing
bounds the aggregate image bytes. No timeout on parsing or rendering.

**Concurrency** — Synchronous and CPU/memory-heavy, called with no `to_thread`/executor from `async def
run_pdf_engine` (`document.py:34`). It blocks the event loop for the whole parse + raster + PNG-encode +
base64 of up to 6 pages. No shared mutable state.

**Complexity hotspots** — `render_pdf` `pdf.py:27` = 41 LOC, cyclomatic ≈ 6. Under both thresholds.

**Findings** — `REL-01` (`ChatRequest.pdf` at `main.py:196` has no size bound and Starlette sets no body
limit, and this module is where that unbounded input is decoded and rasterised). Unassigned:
`core/extract.py:58-61` calls `render_pdf(..., max_pages=10)` — overriding `MAX_PDF_PAGES = 6` — and then
**discards the images** (`_images, text, _total = …`), so ten pages are rasterised, PIL-converted,
PNG-encoded and base64-encoded purely to be thrown away; there is no text-only mode. `RENDER_SCALE = 2.0`
is a multiplier of the page's own size, so output resolution is entirely document-controlled and the
"~144 DPI" comment `:19` holds only for Letter/A4.

---

## extract

**Purpose** — Turn a fetched HTML / PDF / plain-text body into readable text plus a title for the model;
refuse anything else. HTML via trafilatura with a regex fallback; PDF via `core/pdf.py`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_TITLE_RE` / `_TAG_RE` / `_WS_RE` / `_BLANKS_RE` | compiled regexes | `extract.py:17-20` |
| `UnsupportedContentError` | `class(ValueError)` | `extract.py:23` |
| `Extracted` | `@dataclass(title: str, text: str)` | `extract.py:27-30` |
| `_title_from_url` / `_html_title` / `_strip_tags` / `_extract_pdf_text` | | `:33` / `:38` / `:47` / `:55` |
| `extract_readable` | `(content_type: str, body: bytes, url: str) -> Extracted` | [`extract.py:64`](../../orchestrator/app/core/extract.py#L64) |
| `truncate_chars` | `(text: str, max_chars: int) -> str` | `extract.py:100` |

**Control flow** — `extract_readable` normalises the content type to its media type `:69` and lowercases
the URL `:70`, then:

1. **PDF branch** — `"pdf" in ct or lowered_url.endswith(".pdf")` `:72` → `_extract_pdf_text(body)` `:73`,
   which base64-**encodes** the whole body `:60` only for `render_pdf` to base64-**decode** it again
   (`pdf.py:36`), with `max_pages=10`.
2. **Plain-text branch** — exact match on `text/plain` / `text/markdown` `:75-78`, decoded with
   `errors="replace"`.
3. **HTML/XML/empty branch** `:80-95` — decode `:81`; `trafilatura.extract(html, include_comments=False,
   include_tables=True, favor_recall=True)` inside a bare `try/except Exception` `:83-90`; fall back to
   `_strip_tags` when the result is falsy `:91-92`; title from `_html_title` else the URL host `:94`.
4. Anything else → `UnsupportedContentError(ct or "unknown")` `:97`.

**State & side effects** — Pure function of its arguments. No filesystem, DB, env reads or network.
Indirect CPU/memory pressure via pypdfium2 and lxml.

**Dependencies** — Inbound: `engines/url.py:47` (called **inline** in `async def fetch_and_store`),
`:48` (`except UnsupportedContentError`); `engines/search.py:328` (dispatched to `_EXTRACT_POOL`, a
dedicated **single-worker** `ThreadPoolExecutor` at `search.py:60`), `:333`/`:370` (`truncate_chars`).
Tests `test_extract.py:16-58`, `test_url_engine.py:49`, `test_search_engine.py:78`. Outbound: `re`,
`dataclasses`, `typing`, `urllib.parse.urlparse`; lazily `base64` `:56`, `.pdf.render_pdf` `:58`,
`trafilatura` `:84`.

**Config** — **No env vars and no `settings` import.** Every bound is hard-coded here (`max_pages=10`
`:60`), in the caller (`settings.fetch_max_bytes`, default 5 000 000, `config.py:199`, applied at
`engines/url.py:36` and `search.py:315`) or in `core/pdf.py:18-20`.

**Failure modes** — Raises `UnsupportedContentError` at `:97` only. **`_extract_pdf_text` raises
freely**: `binascii.Error`, `pypdfium2.PdfiumError`, `MemoryError`, `ImportError`. `engines/url.py:48`
catches **only** `UnsupportedContentError`, so any of those propagate out of `fetch_and_store` →
`run_url_engine` (`url.py:91`, no try/except) → the generic handler at `main.py:670`, which converts the
whole turn into a terminal `error` event carrying `str(exc)`. `engines/search.py:337-341` does catch bare
`Exception`, so the search path degrades to the provider snippet instead. Swallowed: `except Exception:
text = None` around trafilatura `:89-90` hides `ImportError`, lxml parse aborts and `MemoryError` alike,
silently degrading to the much worse `_strip_tags` output with no signal. **Content-type is trusted**: a
server sending `Content-Type: application/pdf` for 5 MB of arbitrary bytes forces the PDF path `:72`, and
a `.pdf` URL suffix alone is enough even when the body is HTML. No timeout and no size bound inside this
module.

**Concurrency** — Fully synchronous, no shared state. `engines/search.py:325-332` deliberately offloads
to a single-worker executor and documents why (trafilatura's module-level compiled lxml XPath objects are
not thread-safe — `search.py:318-324`). **`engines/url.py:47` calls the same function directly inside an
`async def`**, so it both blocks the event loop and can run trafilatura concurrently with the search
pool's worker — the exact thread-safety hazard the search path was written to avoid.

**Complexity hotspots** — None. `extract_readable` `:64-97` is 34 LOC with 6 branches.

**Findings** — `SEC-05` (this is the conversion point where untrusted remote HTML/PDF becomes prompt
text; nothing here strips instructions or tags provenance), `REL-03` (bare `except Exception` at `:89-90`,
no log). Unassigned: the `_images` variable at `:60` is assigned and never used — the entire raster
pipeline in `pdf.py:50-57` is pure waste on this path.

---

## profile

**Purpose** — Profile an uploaded dataset (shape, dtypes, null rate, cardinality, ranges, capped sample)
with DuckDB / openpyxl so the model is shown statistics rather than the file. Deliberately never reports
min/max *values* for string columns.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `TABULAR_SUFFIXES` / `EXCEL_SUFFIXES` | `{.csv,.tsv,.txt,.parquet,.json,.jsonl,.ndjson}` / `{.xlsx}` | `profile.py:30-31` |
| `clip` | `(value: Any) -> Any` | `profile.py:34` |
| `_STRINGISH` / `_is_stringish` | dtype tokens / `(dtype) -> bool` | `profile.py:45` / `:48` |
| `_duck` | `() -> duckdb.DuckDBPyConnection` | `profile.py:52` |
| `_reader_sql` | `(path) -> str` | `profile.py:73` |
| `profile_tabular` | `(path, *, name=None) -> Dict[str, Any]` | [`profile.py:83`](../../orchestrator/app/core/profile.py#L83) |
| `profile_excel` / `profile_file` / `profile_directory` / `profile_json` | | `:157` / `:208` / `:230` / `:250` |

**Control flow** — `profile_directory(root)` walks the extraction dir `:233`, sorts `:234`, stops at
`settings.profile_max_files` `:235-236`, calls `profile_file` per file `:240`. `profile_file` `:208-227`
refuses `archive.REFUSED_SUFFIXES` `:212-214`; `.xlsx` → `archive.is_zip_container` then `profile_excel`
`:215-218`; tabular suffix **or** `archive.sniff_format(path) == "parquet"` → `profile_tabular` `:219-222`;
else a `{"kind": "other"}` stub `:223-227`.

`profile_tabular` `:83-154`: `_duck()` `:91` connects to `:memory:` `:55` and applies three pragmas —
`autoinstall_known_extensions=false`, `autoload_known_extensions=false`,
`disabled_filesystems='HTTPFileSystem,S3FileSystem'` — **each wrapped in `try/except Exception: pass`**
`:61-69` (the comment at `:56-60` explains that `enable_external_access=false` cannot be used because it
would also block reading the local file). `_reader_sql` `:73-80` picks `read_parquet` / `read_json_auto` /
`read_csv_auto(..., SAMPLE_SIZE=20000, IGNORE_ERRORS=true)` and doubles single quotes in the path `:75`.
Then `SELECT COUNT(*)` `:94`, `DESCRIBE SELECT *` `:95`, columns truncated to `profile_max_columns`
`:98-100`, a per-column loop `:103-140` (nulls + distinct `:107-109`; `MIN/MAX(LENGTH)` for string-ish
dtypes `:119-123` else `MIN/MAX` values `:125-128`; top values when `0 < distinct <= 50` `:130-138`), and
a `LIMIT profile_sample_rows` sample `:143-149`. The whole body is wrapped in `except Exception as exc:
out["error"] = f"could not be read as a table: {type(exc).__name__}"` `:150-151`; `con.close()` in
`finally` `:152-153`.

`profile_excel` `:157-205` **re-asserts** `archive.check_zip_container(path, label="spreadsheet")` `:167`
before importing openpyxl `:169`, loads read-only `:178`, iterates the first 10 worksheets `:180`, and
then **iterates every remaining row purely to count them** while keeping only `profile_sample_rows`
samples `:189-194`.

**State & side effects** — Filesystem reads: `os.path.getsize` `:88,173,226`, DuckDB reading the profiled
file `:78-80`, `openpyxl.load_workbook` `:178`, `os.walk` `:233`. **No filesystem or DB writes here** —
the caller persists at `uploads.py:141-149`. **Network egress: none by design** — `_duck` `:52-70` is the
control that prevents DuckDB reaching HTTP/S3, but see Failure modes. No GPU, no module-level mutable
state, no direct env reads.

**Dependencies** — Inbound: `uploads.py:26` (import), `:121` `profile_directory(extract_dir)`, `:147`
`profile_json(profiles)`; tests `test_dataset_profile.py:51,67,75,81,101,114,142,248`,
`test_archive_safety.py:204,302`. Outbound: `json`, `os`, `typing`, `..config.settings` `:27`,
`.archive` `:28`; lazily `duckdb` `:53` and `openpyxl.load_workbook` `:169`.

**Config** — `PROFILE_CELL_CHARS` (200, `config.py:185`) at `:41`; `PROFILE_MAX_COLUMNS` (60,
`config.py:188`) at `:98`/`:186`; `PROFILE_TOP_VALUES` (5, `config.py:186`) at `:134`;
`PROFILE_SAMPLE_ROWS` (5, `config.py:184`) at `:144`/`:191`; `PROFILE_MAX_FILES` (40, `config.py:187`) at
`:235`; indirectly the `ARCHIVE_MAX_*` limits via `check_zip_container` `:167`. **None of the `PROFILE_*`
vars appears in `.env.example`, and none is forwarded by `docker-compose.yml:219-267`.**

**Failure modes** — Raises `archive.ArchiveError` from `:167`, openpyxl exceptions from `:178`/`:189`,
and `OSError` from the unguarded `os.path.getsize` at `:173` (unlike `:88` and `:226`, which test
`os.path.exists` first). **Four separate bare `except Exception`**, none of which logs — the module
contains no logging at all:

| Line | What is swallowed | Consequence |
|---|---|---|
| `:68-69` | a failed **security pragma** | if DuckDB rejects `SET disabled_filesystems=…` (older build, different spelling), profiling runs with the HTTP and S3 filesystems **live**, silently |
| `:139-140` | every per-column statistic failure | becomes `stats_unavailable = True` with no reason |
| `:150-151` | the whole tabular profile | degrades to `{"error": "could not be read as a table: <Type>"}` |
| `:243-246` | one bad file in `profile_directory` | only the exception class name is kept |

No timeout on any DuckDB query (`:94,95,107,119,125,131,143`) and no interrupt hook. No row-count bound on
`profile_excel`'s counting loop `:189-194`. `read_csv_auto(..., IGNORE_ERRORS=true)` `:80` silently drops
malformed rows, so `out["rows"]` under-reports with no signal. `_reader_sql` escapes `'` → `''` `:75`,
which is the correct DuckDB escape (DuckDB standard string literals have no backslash escape), so
extracted filenames cannot break out of the literal.

**Concurrency** — Fully synchronous, called from `async def create_upload` (`uploads.py:121`), so the
**entire profiling run blocks the event loop**. Each call gets a fresh `:memory:` connection closed in
`finally`, so there are no intra-module races — but nothing limits concurrent uploads, so N simultaneous
uploads each open their own in-memory DuckDB.

**Complexity hotspots** — `profile_tabular`
[`profile.py:83-154`](../../orchestrator/app/core/profile.py#L83) = **72 LOC, cyclomatic ≈ 12** — the
largest function in the data half of the package. `profile_excel` `:157-205` = 49 LOC, 6 branches.

**Findings** — `REL-03` (four unlogged bare `except Exception`, one of them on a security pragma at
`:68-69`). Unassigned: `src` `:93` is an inline table function, not a materialised table, so **every**
`con.execute` re-reads and re-parses the source file — with the default `profile_max_columns = 60` a CSV
is scanned roughly **2 + 60 × 2..3 + 1 ≈ up to 183 full passes**. The module's security contract in the
header comment `:1-20` (only `sample_rows` and `top_values` carry raw data, both through `clip`; string
min/max *values* deliberately absent) **is met by the implementation** — verified line by line at
`:119-128` and `:136-138`, and asserted by `tests/test_dataset_profile.py:101-142`.

---

## report_paths

**Purpose** — Resolves a user-supplied report filename inside `REPORTS_DIR` for
`GET /reports/{filename}`, and lists the directory for `GET /reports`.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `ReportPathError` | `class(ValueError)` | `report_paths.py:19` |
| `resolve_report_file` | `(reports_dir: str \| Path, filename: str) -> Path` | [`report_paths.py:23`](../../orchestrator/app/core/report_paths.py#L23) |
| `list_reports` | `(reports_dir: str \| Path) -> List[dict]` — `{"filename","size_bytes","modified"}` | `report_paths.py:51` |

**Control flow** — `resolve_report_file`: empty/whitespace reject `:28-29`; `name = filename.strip()`
`:30`; `name in {".",".."} or ".." in name` reject `:31-32`; `"/" in name or "\\" in name` reject `:33-34`;
`Path(name).is_absolute()` reject `:35-36` (unreachable on POSIX after step 3); leading `.` reject
`:37-38`; NUL reject `:39-40`; `base = Path(reports_dir).resolve()` `:42`;
`resolved = (base / name).resolve()` `:43` — **`resolve()` follows symlinks**;
`resolved.is_relative_to(base)` else reject `:46-47`. Existence is deliberately not checked here (`:26`);
the caller decides at `main.py:267-268`.

`list_reports`: missing dir ⇒ `[]` `:53-55`; `sorted(base.iterdir())` `:57`; skip non-files and dotfiles
`:58-59`; `p.stat()` `:60`; UTC ISO mtime `:61-67`; re-sort newest first `:68`.

**State & side effects** — Filesystem **reads only**: `resolve()` `:42-43`, `is_dir` `:54`, `iterdir`
`:57`, `is_file` `:58`, `stat` `:60`. No writes, network, DB or env reads.

**Dependencies** — Inbound: `main.py:19` (import), `:259` `list_reports(settings.reports_dir)` for
`GET /reports` (`main.py:257-259`), `:265` `resolve_report_file(...)` for `GET /reports/{filename}`
(`main.py:262-271`). Tests `tests/test_report_paths.py`. Outbound: `datetime`, `pathlib.Path`, `typing`.

**Config** — No direct env reads. `settings.reports_dir` (`REPORTS_DIR`, default `/reports`,
`config.py:100`) is injected by `main.py:259`/`:265`.

**Failure modes** — Raises only `ReportPathError`, mapped to HTTP 400 at `main.py:266-267`.
`list_reports` has **no `try/except`**: a file removed between `iterdir()` `:57` and `stat()` `:60` raises
`FileNotFoundError` out of the route → HTTP 500; a `PermissionError` on `iterdir` likewise. No bound on
the number of entries returned — the whole directory is serialised in one response.

**Concurrency** — Pure sync, called from `async def reports_index` / `async def get_report`
(`main.py:257`, `:262`), so the directory scan runs on the event loop. There is a TOCTOU window between
`resolve()` `:43` and `FileResponse(path)` at `main.py:270`, only exploitable by something that can
already create symlinks inside `REPORTS_DIR`.

**Complexity hotspots** — None. `resolve_report_file` = 26 LOC, ~9 decision points; `list_reports` = 19 LOC.

**Findings** — `SEC-01` (neither `main.py:257-259` nor `:262-271` declares any dependency; `main.py:55-56`
states "/chat and /reports* remain auth-free", and the orchestrator is published on `0.0.0.0:8080` at
`docker-compose.yml:272-273`). The path logic itself is sound — see
[security-model.md](security-model.md) §c for the covered/not-covered assessment. Unassigned: there is
**no extension allowlist**, so every regular file in `REPORTS_DIR` is downloadable, and that directory
receives both `.docx`/`.pdf` reports (`engines/report.py:256`) and CSV/XLSX exports of warehouse query
results (`engines/sql.py:407-409`).

---

## urls

**Purpose** — Finds pasted http(s) links in a user message and reduces a large fetched page to the
portion most relevant to the question, by keyword overlap, before it enters the model prompt.

**Public surface**

| Symbol | Signature | `file:line` |
|---|---|---|
| `_URL_RE` | `re.compile(r"https?://[^\s<>\"')\]]+", re.I)` | `urls.py:16` |
| `_STRIP_TRAILING` | `".,;:!?)]}\"'"` | `urls.py:17` |
| `extract_urls` | `(text: str, limit: int = 5) -> List[str]` | `urls.py:20` |
| `chunk_text` | `(text: str, chunk_chars: int = 1600, overlap: int = 200) -> List[str]` | `urls.py:32` |
| `select_relevant` | `(text: str, query: str, max_chars: int) -> str` | [`urls.py:52`](../../orchestrator/app/core/urls.py#L52) |

**Control flow** — `extract_urls`: `findall` `:23` → `rstrip(_STRIP_TRAILING)` `:24` → order-preserving
dedupe `:25-26` → break at `limit` `:27-28`. `chunk_text`: short-circuit for small text `:34-35`; loop
`:39-48` taking `chunk_chars`, backing up to the last space after the 60 % mark `:41-44`, advancing
`start = max(end - overlap, start + 1)` `:48` (progress guaranteed). `select_relevant`: passthrough when
`len(text) <= max_chars` `:59-60`; `keywords(query, max_keywords=12)` `:61`; no keywords ⇒ head truncation
`:62-63`; `chunk_size = min(1600, max(300, max_chars // 2))` `:66`; chunk `:67`; score each chunk by
`sum(low.count(k) for k in kws)` `:70-73`; sort by score desc `:75`; greedily fill the budget `:78-82`;
restore reading order `:83`; join with `"\n…\n"` `:84`; hard truncate `:85`.

**State & side effects** — None. Pure functions, no I/O, no globals, no env reads.

**Dependencies** — Inbound: `main.py:488` (import), `:490`
`extract_urls(request.text, limit=settings.url_max_pages)`, `:499`
`select_relevant(d["text"], request.text, 6000)`; `engines/url.py:18`, `:62`
`select_relevant(d["text"], question, share)`. Tests `tests/test_urls.py`. Outbound: `re`, `typing`, and
**`..memory_recall.keywords` (`urls.py:13`) — an app-level import that breaks the package's stdlib-only
contract** (though `memory_recall` itself imports only `re` and `typing`, `memory_recall.py:12-13`).

**Config** — No direct env reads. `settings.url_max_pages` (`URL_MAX_PAGES`, default 5, `config.py:208`)
is applied by the caller at `main.py:490`.

**Failure modes** — Nothing raises deliberately; nothing is caught. `extract_urls(None)` is safe
(`text or ""` at `:23`). `select_relevant` has no guard for `max_chars <= 0` — `chunk_size` floors to 300,
the greedy loop `:79` picks exactly one chunk, and `joined[:0]` returns `""`. **No length cap on a single
"URL"**: `_URL_RE` will match a 100 KB token, which is then handed to `net.assert_url_is_fetchable`.

**Concurrency** — Pure sync, thread-safe. **Called inline from the async request path** (`main.py:499`)
and from `async def run_url_engine` via `_context_block` (`engines/url.py:62`), so the chunk/score pass
over up to `fetch_max_bytes` (5 MB) of extracted text runs on the event loop. For a 5 MB page that is
~3 200 chunks × 12 keywords × `str.count` — measurable but not pathological.

**Complexity hotspots** — None. Largest function `select_relevant` = 34 LOC, ~7 decision points.

**Findings** — `SEC-05` (this module selects which slice of untrusted fetched text is spliced into the
prompt, and performs no instruction-stripping, delimiting or provenance tagging; the caller at
`main.py:502-510` then re-injects that text as a **`system`** message on every subsequent turn, and
`engines/__init__.py:16-19` keeps every system message forever by design). This module performs **no
URL validation or normalisation of its own** — it does not lowercase the host, strip fragments,
percent-decode, punycode-encode, reject userinfo or bound length; scheme/host/DNS are handled downstream
by `net.py:94-99`, and length/normalisation are handled nowhere. Unassigned: `_URL_RE` deliberately stops
at `)` and `]` (`:16`), so `https://en.wikipedia.org/wiki/Salesforce_(company)` is silently truncated to
`https://en.wikipedia.org/wiki/Salesforce_`; and `chunk_text` here duplicates
`sync-worker/syncworker/chunking.py:14 chunk_text` (token-based, different semantics) — two independent
chunkers in the monorepo.
