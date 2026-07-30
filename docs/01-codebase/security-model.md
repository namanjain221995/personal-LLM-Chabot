# Security model

The honest summary: **this platform has no authentication, and every application and model port is
published on all interfaces.** What it does have is a small set of genuinely well-built input guards —
two of them excellent, one of them provably weaker than its own docstring claims — plus disciplined
secret hygiene in git. The system is safe today because it sits behind a workstation's network
boundary and because two DuckDB connection flags never regressed. Neither is a security control the
codebase owns.

---

## (a) Authentication & authorization

**There is none.** This is stated in the source, not inferred:

| Fact | Evidence |
|---|---|
| `require_user` is a FastAPI dependency that **cannot fail** — docstring: *"Never 401s now."* | [`auth.py:95-97`](../../orchestrator/app/auth.py#L95) |
| `current_user` discards the `Request` (`del request  # no cookie is read — there is no session any more`) and returns `local_user()` | `auth.py:89-92` |
| Module header: *"there is now no authentication whatsoever. Anyone who can reach the port can read every conversation and query the Salesforce data."* | `auth.py:17-20` |
| `/chat` and `/reports*` are declared auth-free in a code comment | [`main.py:55-56`](../../orchestrator/app/main.py#L55) |
| The single local account is auto-created on first request with the literal password hash `"!local-no-login"` | `auth.py:77-86`, `:79` |
| `GET /auth/me` returns `{"username": …, "local": True}` — a display value, explicitly *"not a login check"* | `auth.py:100-103` |

### What survives

`user_id` scoping is still **plumbed** end to end. `db.conversation_owner`, `db.get_uploads`,
`db.get_url_documents`, `db.get_repo_keys` and every `/history/*` route are written as if identity
mattered, and the `Depends(require_user)` markers are present on all of `history.py` and `uploads.py`.
The wiring is intact; only the identity source is degenerate. That is a meaningful architectural
asset — restoring authentication is a change to `auth.py`, not a refactor of the data layer.

The one place the scoping is actually *checked* is the conversation-ownership gate in `POST /chat`:

```
    try:
        conv_owner = db.conversation_owner(conv_key_outer)
    except Exception:
        conv_owner = None
    viewer = int(signed_in["id"]) if signed_in is not None else None
    if conv_owner is not None and conv_owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")
```
— [`main.py:338-344`](../../orchestrator/app/main.py#L338)

The comment above it (`main.py:330-337`) states the intent precisely: the per-conversation stores
(`url_documents`, `repo_chunks`) and the live-generation registry are keyed by conversation id alone,
so without this check "anyone who guessed an id could pull another account's fetched pages and indexed
source code into their own prompt".

**`SEC-02` — the check falls OPEN.** The `except Exception: conv_owner = None` at `main.py:340-341`
means *any* DB failure sets `conv_owner = None`, and `None` skips the comparison at `main.py:343`. The
in-code justification ("If the DB is unreachable this raises; the stores it guards are read through
the same connection, so they fail too and nothing can leak", `main.py:336-337`) is an assumption about
correlated failure, not an enforcement. A `sqlite3.OperationalError` on a locked row, a `TypeError`
from an unexpected shape, or any narrower fault that does **not** also break the subsequent reads,
opens the gate. The correct shape is fail-closed: catch, log, and 503.

Two further authorization surfaces are tautologies today, and will become real bugs the moment
identity is restored:

- `_owns(gen, viewer)` at `main.py:701-709` compares `gen.user_id` to `_viewer_id(request)`; since
  every caller resolves to the same `local_user()` id, `/chat/attach`, `/chat/stop` and `/chat/active`
  are open to any client that can reach the port.
- `if user is None: raise HTTPException(401)` at `main.py:756-757` is **unreachable** — `current_user`
  never returns `None` (`auth.py:92`).

Dead auth scaffolding still present: `SESSION_COOKIE = "ts_session"` (`auth.py:35`) has no reader;
`argon2-cffi` and `itsdangerous` remain installed dependencies (`orchestrator/requirements.txt`, "V2
auth" block); `main.py:46` still comments "allow_credentials so the ts_session cookie flows";
`SESSION_SECRET` is forwarded by `docker-compose.yml:249` and read by nothing (`config.py:259` reads
`SESSION_SECRET_FILE` only).

There is **no rate limiting, no CSRF token, no bearer token and no mTLS** anywhere — the only
auth-shaped dependency in the codebase is `Depends(require_user)`, and it never fails
(`auth.py:95-97`); no other middleware is registered beyond CORS (`main.py:47-53`). The one rate limit
in the system is `search.rate_ok(user_key)` (`main.py:437`), which bounds **outbound** web-search
calls, not inbound requests, and keys on `"anon"` when there is no user.

---

## (b) Network exposure

Every port uses the short `"HOST:CONTAINER"` compose syntax with **no host-IP prefix**, which binds
`0.0.0.0`. There is no `networks:` section in the file at all, so all seven services share the default
project bridge and any container can reach any other.

| Service | Publish | Bind | Container port | Authenticated? | Evidence |
|---|---|---|---|---|---|
| `vllm` (main model) | `8000:30000` | **0.0.0.0** | 30000 | **No** | `docker-compose.yml:85-86` |
| `vllm-vision` (profile `vision`) | `8001:30001` | **0.0.0.0** | 30001 | **No** | `docker-compose.yml:132-133` |
| `vllm-router` | `8002:30002` | **0.0.0.0** | 30002 | **No** | `docker-compose.yml:170-171` |
| `vllm-embed` | `8003:30003` | **0.0.0.0** | 30003 | **No** | `docker-compose.yml:201-202` |
| `orchestrator` | `8080:8080` | **0.0.0.0** | 8080 | **No** | `docker-compose.yml:272-273` |
| `frontend` | `3000:3000` | **0.0.0.0** | 3000 | **No** | `docker-compose.yml:351-352` |
| `searxng` (profile `search`) | none | — | 8080 internal | n/a | `docker-compose.yml:336-344` (no `ports:`) |
| `sync-worker` | none | — | — | n/a | `docker-compose.yml:291-331` |

**No vLLM service is started with an API key.** `rg -n 'api-key|api_key|OPENAI_API_KEY'
docker-compose.yml` returns exactly one line — `OPENAI_API_KEY: local-no-key` at
`docker-compose.yml:230`, which is the *client-side* placeholder the orchestrator sends, not a server
credential. The four model servers accept any request. `COST-01` follows directly: anyone reachable on
ports 8000/8001/8002/8003 gets unmetered inference on the GB10's 121 GB of unified memory, and the
orchestrator's own GPU reservation (`docker-compose.yml:283-289`) sits outside that budget.

`searxng` is the one service done right on this axis: internal-only, reachable solely at
`http://searxng:8080` from the orchestrator. It does carry a separate weakness —
`SEARXNG_SECRET: ${SEARXNG_SECRET:-please-change-me}` (`docker-compose.yml:339`) starts with a
publicly-known secret when the operator forgets the variable.

### CORS is correct — and does not help

```
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```
— [`main.py:47-53`](../../orchestrator/app/main.py#L47), with
`cors_allow_origins` defaulting to `["http://localhost:3000", "http://127.0.0.1:3000"]`
([`config.py:244-250`](../../orchestrator/app/config.py#L244)).

This is a tight, correctly-scoped allowlist, and the comment at `main.py:43-45` shows the author
understood exactly why ("A wildcard here would let any web page the user visits cross-origin read
/reports and drive /chat against the synced Salesforce data"). **But CORS is a browser-enforced
control.** It stops a malicious web page the operator visits; it does nothing about `curl`, a script
on the LAN, or any non-browser client. With no authentication behind it, the effective access-control
boundary for `http://<host>:8080/chat` and `http://<host>:8080/reports` is the network the workstation
is plugged into.

`GET /health` (`main.py:242-254`) is likewise unauthenticated and returns per-dependency check results.

`SEC-01` is the aggregate of everything in this section, and it is the only P0 in the audit.

---

## (c) The four guard modules, assessed

The `core` package contains four modules whose entire purpose is to make untrusted input safe. Two are
excellent, one is good with a known TOCTOU, and one is provably weaker than it claims.

A structural observation first: **`sql_guard` and `archive` enumerate the bad (blocklists); `net` and
`report_paths` enumerate the good (allowlists).** The two blocklist modules are the two with
demonstrated gaps.

### `sql_guard` — the weak one

**What it genuinely covers**, verified by reading and by the existing test suite:

- Comment-smuggled keywords: `UPD/**/ATE`, `DR--\nOP`. The `_scan` loop emits comment bodies into
  `cleaned` but **nothing** into `bare` (`sql_guard.py:75-85`), so the fragments reassemble in `bare`
  and hit the blocklist at `:146`.
- Keywords inside ordinary string literals do **not** false-positive — literal bodies are stripped
  from `bare` (`sql_guard.py:87-103`).
- Column names such as `update_date`, `payload`, `offset_value` do not false-positive — word
  boundaries (`tests/test_sql_guard.py:32`).
- Plain stacked statements `SELECT 1; DROP TABLE t` — `sql_guard.py:142`.
- All 27 leading write/DDL keywords (`INSERT/UPDATE/DELETE/ATTACH/COPY/PRAGMA/CREATE/DROP/ALTER/
  INSTALL/LOAD/SET/CALL/EXPORT/IMPORT/TRUNCATE/VACUUM/MERGE/GRANT/REVOKE/CHECKPOINT/USE/BEGIN/COMMIT/
  ROLLBACK`) — `sql_guard.py:30-37` + `:144`.
- A CTE that ends in a write (`WITH x AS (SELECT 1) INSERT INTO …`) — `:146`,
  `tests/test_sql_guard.py:137-138`.
- Un-quoted `read_csv(` / `read_parquet(` / `glob(` / `read_blob(` and 18 others, including
  comment-split forms — `sql_guard.py:45-54` + `:149`.
- **Complete coverage of the execution path**: `rg -n "duckdb.connect|guard_sql|is_safe_select"` over
  `orchestrator/` finds `guard_sql` at exactly two call sites, `engines/sql.py:200` (first attempt) and
  `engines/sql.py:205` (retry). Both branches of `generate_and_run_sql` guard before `_execute`;
  `report.py:127` and `agent.py:257` reach DuckDB only through `generate_and_run_sql`. **There is no
  path that executes model-generated SQL without the guard.**

**`SEC-07` — what it does not cover. Confirmed by execution against the real module.**

The design splits the input into two buffers: `cleaned` (what gets executed, `sql_guard.py:156-157`)
and `bare` (what gets checked, `sql_guard.py:139-154`). `_scan` does not model PostgreSQL/DuckDB `E'…'`
escape strings: it reads `\` as an ordinary body character, then treats the following `''` as an
*escaped quote* (`sql_guard.py:93-96`) and **stays inside the literal**, while DuckDB closes the
literal at the same point. From there the two buffers are desynchronised — everything after lands in
`cleaned` (executed) but is stripped from `bare` (checked).

Reproduced on this machine with `orchestrator/.venv/bin/python`:

| Payload | Guard verdict | Why it matters |
|---|---|---|
| `SELECT E'\'', * FROM read_csv('/etc/passwd')` | **PASSES** | defeats `_FORBIDDEN_TABLE_FUNCS` (`:149`) |
| `SELECT E'\'' , 1; DROP TABLE t` | **PASSES** | defeats the multi-statement check (`:142`) *and* the keyword blocklist (`:146`) with one input |
| `SELECT e'\'' AS a, * FROM accounts; ATTACH '/tmp/x.db' AS z` | **PASSES** | same, lower-case `e` prefix |

**The guard's own promise is therefore false as written.** `sql_guard.py:144` raises "only a single
SELECT (or WITH ... SELECT) statement is allowed", and the module docstring at `sql_guard.py:9-13`
claims it "rejects DuckDB filesystem/network table functions". Both claims are one bypass deep.

**And DuckDB blocks every one of them.** Also confirmed by execution, against a connection opened with
the exact production config:

```
con = duckdb.connect(
    settings.duckdb_path,
    read_only=True,
    config={
        "enable_external_access": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    },
)
```
— [`engines/sql.py:124-132`](../../orchestrator/app/engines/sql.py#L124)

| Payload after `guard_sql` | DuckDB result |
|---|---|
| `SELECT E'\'', * FROM read_csv('/etc/passwd')` | `PermissionException: Cannot access file "/etc/passwd" - file system operations are disabled by configuration` |
| `SELECT E'\'' , 1; DROP TABLE t` | `InvalidInputException: Cannot execute statement of type "DROP" on database "t" which is attached in read-only mode!` |
| `SELECT (SELECT count(*) FROM '/etc/passwd') AS leak` | `CatalogException: Table with name /etc/passwd does not exist!` |

The comment at `engines/sql.py:119-122` shows this was deliberate and understood: "DuckDB's
read_csv/read_blob/glob/httpfs table functions would otherwise let a guard-approved SELECT read
arbitrary host files or reach the network. With this config DuckDB raises PermissionException for
every such function."

**Residual risk, stated precisely.** `SEC-07` is **not currently exploitable**. It is not a
vulnerability today; it is a **single point of failure**. The whole safety argument for model-authored
SQL now rests on two connection flags at `engines/sql.py:126-129` never regressing — not on the guard,
whose docstring contract is demonstrably false. A future edit that opens a second DuckDB handle for
model SQL without both flags (`schema_cache.py:40-48` already duplicates the config by hand, and
`core/profile.py:52-70` deliberately *cannot* use `enable_external_access=False`, applying three
pragmas each wrapped in `except Exception: pass` at `:61-69` instead) converts a latent parser defect
into arbitrary host-file read. The correct remediation is to stop hand-rolling a SQL parser: either
model `E'…'`/`$$…$$` in `_scan`, or replace the scanner with a real parse (DuckDB's own
`json_serialize_sql`), and in either case treat the connection flags as the primary control and say so
in the docstring.

Two further limits are worth stating because they are outside the guard's remit and nothing else
supplies them: **there is no cost bound and no timeout.** `WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL
SELECT n+1 FROM r) SELECT count(*) FROM accounts, r` and a 5-way cartesian product both pass, and
`engines/sql.py:117-139` sets no DuckDB interrupt — on a single-threaded event loop that is a
one-request denial of service.

`QUAL-02`: `is_safe_select` (`sql_guard.py:160`) has no production caller — only
`tests/test_sql_guard.py:28,32,43,131`.

### `net` — good, with a known TOCTOU

**Covered**, all verified by execution in the evidence pass on Python 3.12.3:

- Non-http(s) schemes (`file:`, `gopher:`, `ftp:`) — `net.py:94-95`, `tests/test_net_ssrf.py:34-37`.
- IPv4 private / loopback / link-local / reserved / multicast / unspecified — `net.py:47-55`.
  `169.254.169.254` (cloud metadata) and `0.0.0.0` are blocked.
- **IPv6**: `::1`, `fd00:ec2::254`, `::ffff:127.0.0.1`, `::ffff:169.254.169.254` — all blocked.
- **Alternate IPv4 encodings**: `2130706433`, `0177.0.0.1`, `127.1` are rejected as literals by
  `ipaddress.ip_address`, fall through to `getaddrinfo`, resolve to `127.0.0.1`, and are then blocked
  by the resolved-IP check at `net.py:82-86`. Decimal/octal/short-form are **covered**.
- Userinfo spoofing `http://public.example@127.0.0.1/` — `parsed.hostname` drops the userinfo
  (`net.py:96`).
- Multi-A-record rebinding (one name → public **and** private records) — rejected if *any* address is
  blocked, `net.py:82-86`, `tests/test_net_ssrf.py:60-65`.
- Redirect into private space — **re-validated on every hop** at `net.py:147`,
  `tests/test_net_ssrf.py:79-106`; chains longer than 3 rejected at `net.py:135`/`:162`.

**Not covered:**

- **`SEC-03` — resolve-then-connect TOCTOU / DNS rebinding.** `resolve_public_ips` returns the
  validated IPs (`net.py:87`) and `assert_url_is_fetchable` **throws them away** (`net.py:99`, the call
  is a bare statement). httpx then performs its own, second resolution when it connects at
  `net.py:137`. Nothing pins the address, so a hostile authoritative server with a 0-second TTL can
  answer public on the check and private on the connect. The fix is to pin: resolve once, connect to
  the literal, and pass the hostname via SNI/`Host`.
- **`PERF-02`** — `client.get()` on httpx 0.28.1 buffers the entire response body before returning
  (`net.py:137`); the `max_bytes` cap at `net.py:153-155` runs afterwards. A malicious host can force
  the orchestrator to allocate an unbounded body regardless of `FETCH_MAX_BYTES`.
- **Carrier-grade NAT `100.64.0.0/10` is fetchable.** `ipaddress.ip_address('100.64.1.1').is_private`
  is `False` on Python 3.12.3, and `_ip_is_blocked` (`net.py:47-55`) lists no explicit networks. That
  is the Tailscale range.
- **No destination-port restriction** — `http://public-host:6379/` is allowed.
- **No total deadline.** The `read` timeout is per-read (`net.py:124-126`), so a slowloris trickling
  one byte every 7 s survives an 8 s read timeout indefinitely. `socket.getaddrinfo` (`net.py:76`) has
  no timeout and runs on the default executor, so a black-holed resolver parks a pool thread and, at
  `min(32, cpu+4)` workers, can starve every other `asyncio.to_thread` user in the process.
- `assert_url_is_fetchable`'s docstring promises "the normalized URL" (`net.py:91-92`) and returns
  `url` unchanged (`net.py:100`). **No normalisation happens anywhere in the fetch path** — `core/urls.py`
  does none either.

One thing `net.py` gets right that no other module in the package does: both blocking DNS calls are
correctly off-loaded with `asyncio.to_thread` (`net.py:121`, `:147`), and `CHANGELOG.md:99` records
this as a deliberate fix for an event-loop stall.

**`core/repo.py` bypasses this module entirely.** `https://api.github.com/repos/{owner}/{repo}`
(`repo.py:137`) and `git clone https://github.com/{owner}/{repo}.git` (`repo.py:67`, `:180`) are
hard-coded, unauthenticated egress that never touches `net.py`. Hard-coded hosts make that
defensible as SSRF, but it is real outbound internet traffic in a system documented as local-only,
and `subprocess.run` at `repo.py:182` inherits `dict(os.environ)` (`repo.py:165-172`) — the entire
parent environment, `SF_CLIENT_SECRET` and `HF_TOKEN` included — into the `git` child.

### `archive` — genuinely excellent

This is the strongest guard in the codebase and deserves to be described as such. It is defence in
depth done properly: independent checks at plan time and at write time, four orthogonal bomb caps, and
a structural invariant that makes an entire class of attack impossible.

**Covered:**

| Attack | Control | Evidence |
|---|---|---|
| Absolute paths, `C:\x`, UNC `\\srv\s`, `..` at any depth, NUL/control chars, over-long names/paths | `safe_member_name` | `archive.py:96-112` |
| Backslash-as-separator | normalised | `archive.py:102` |
| Unicode separator manufacture | **NFC** normalisation only — NFC can never produce an ASCII `/` (NFKC could; it is deliberately not used) | `archive.py:102` |
| Zip symlinks | Unix mode bits, `info.external_attr >> 16` + `stat.S_ISLNK` | `archive.py:153-156` |
| Zip symlinks in archives carrying **no** Unix attributes | **structurally impossible** — `_write_member` only ever calls `open(dest,"wb")`, so this extractor can never create a symlink and no later member can be redirected through one | `archive.py:195` |
| Tar symlinks, hardlinks, char/block devices, FIFOs | one `if not member.isfile()` test | `archive.py:251-253` |
| Zip-slip | **checked twice** — `safe_member_name` at plan time, then `resolves_inside(dest, name)` immediately before every write | `archive.py:157-160`, then `:223` (zip) / `:266` (tar) |
| Decompression bombs | **four independent caps**: member count, per-member ratio, header total, and a **live streaming byte budget that does not trust the header** | `:145`/`:243`, `:166-172`, `:173-178`/`:272-277`, `:200-208` |
| Renamed files picking their own reader | magic-byte `sniff_format`, used at `uploads.py:98/101/109` and `profile.py:216/219` | `archive.py:74-89` |
| `.xlsx` bombs | `.xlsx` is a zip and is forced through the same caps before openpyxl sees it, then re-asserted | `uploads.py:110`, `profile.py:167` |
| Nested archives | listed, never opened | `archive.py:220-221`, `:262-265` |
| Code-executing formats | `REFUSED_SUFFIXES = {.pkl,.pickle,.pkl.gz,.xlsm,.xlsb,.pyc,.so}` | `archive.py:38`, `:126-131`, `profile.py:212-214` |

**Not covered:**

- **The per-member ratio cap is never applied to tar/tar.gz** — `max_ratio` is unpacked at
  `archive.py:235` and then unused, a dead binding. Only the total cap and the streaming budget protect
  a `.tar.gz`.
- **The ratio check exempts every member ≤ 1 MiB** (`archive.py:168` requires
  `info.file_size > 1024*1024`), so a many-small-members bomb is bounded only by the 2048 MB total.
- **The extraction byte budget is per-call, not per-user or global** (`archive.py:216`) — N concurrent
  uploads can each spend the full `ARCHIVE_MAX_UNCOMPRESSED_MB`.
- **`ARCHIVE_MAX_DEPTH` (`config.py:182`) is named in the module docstring (`archive.py:14`) and never
  read** — depth 1 is hard-coded by the `continue` at `:221`/`:265`. The behaviour is right; the config
  is a lie.
- **No wall-clock cap** on extraction, and it runs **synchronously on the event loop** from
  `async def create_upload` (`uploads.py:99`/`:103`/`:110`).
- Exceptions other than `BadZipFile`/`TarError` escape uncaught (`KeyError` from `zf.open` at
  `archive.py:226` when `safe_member_name` rewrote the name, `RuntimeError` on encrypted members,
  `NotADirectoryError` from `os.makedirs`). All are absorbed by the blanket `except Exception` at
  `uploads.py:128` and reported as HTTP 400 — a wrong status code, not a leak.
- `resolves_inside`'s docstring claims the check is "re-checked at write time too"
  (`archive.py:118-120`); it is evaluated once per member *before* the write, never inside
  `_write_member`.

### `report_paths` — genuinely excellent, guarding an unauthenticated route

The path logic is the best in the repo. `resolve_report_file` (`report_paths.py:23-48`) rejects in
order: empty/whitespace `:28`, `.`/`..`/any `..` substring `:31`, `/` or `\` `:33`, absolute paths
`:35`, leading `.` `:37`, NUL `:39` — and then, having already excluded every syntactic escape, does
the semantic check anyway: `base.resolve()` / `(base / name).resolve()` / `is_relative_to(base)`
(`:42-47`).

**Covered:** `..`, `../../etc/passwd`, `..%2f` (the literal `..` substring is rejected before any
decoding, and FastAPI has already percent-decoded the path parameter), nested paths, backslash paths,
absolute paths, dotfiles, NUL bytes, and — because `resolve()` follows symlinks before the containment
test — **symlink escape** (`tests/test_report_paths.py:49-55`). A symlink pointing *inside*
`REPORTS_DIR` is correctly allowed (`tests/test_report_paths.py:58-63`).

**Not covered:**

- **`SEC-01`. Neither route it serves has any authentication.** `GET /reports` (`main.py:257-259`) and
  `GET /reports/{filename}` (`main.py:262-271`) declare no `Depends`; `main.py:55-56` says so
  explicitly. A perfect path resolver in front of an open door.
- **No extension allowlist** — every regular file in `REPORTS_DIR` is downloadable, and that directory
  receives both generated `.docx`/`.pdf` reports (`engines/report.py:256`) **and CSV/XLSX exports of
  warehouse query results** (`engines/sql.py:407-409`). There is also no retention or size cap on the
  directory anywhere in `orchestrator/app/`.
- `list_reports` has no `try/except`: a file removed between `iterdir()` (`:57`) and `stat()` (`:60`)
  raises `FileNotFoundError` out of the route → HTTP 500.
- A TOCTOU window exists between `resolve()` (`:43`) and `FileResponse(path)` (`main.py:270`), only
  exploitable by something that can already create symlinks inside `REPORTS_DIR`.

---

## (d) Secret handling

### Done right

| Control | Evidence |
|---|---|
| `.gitignore` covers `.env`, `.env.*`, `!.env.example`, `secrets/`, `*.pem`, `*.key`, `*.p12`, `.session_secret` | `.gitignore:10-17` |
| `*.bak`, `*.bak-*`, `*.orig`, `*.rej`, `*~` are ignored, with a comment naming the exact incident it prevents (`cp .env .env.bak-123456`) | `.gitignore:46-50`, rationale `:4-8` |
| **Nothing sensitive is tracked** — `git ls-files \| rg 'bak\|secrets/\|\.env'` returns only `.env.example` | verified |
| `git check-ignore -v` confirms the patterns fire in place: `.env` ← `:10`, `.env.bak-205921` ← `:47`, `docker-compose.yml.bak-preperf` ← `:47`, `secrets/` ← `:13` | verified |
| **The Salesforce JWT signing key is a mounted file, not an env var** — deliberately, to keep it out of `docker inspect`, shell history and screenshots | `docker-compose.yml:307-311` (comment) + the `:328` read-only mount `${SF_PRIVATE_KEY_HOST_FILE:-/dev/null}:/run/secrets/sf_jwt_key.pem:ro` |
| `.env.example` ships with every sensitive key **empty** — no values are committed | `.env.example:5,8-10,14,52-54` |
| The Salesforce token endpoint's error body is deliberately **not** echoed into the exception | `core/salesforce.py:132-136` |
| Salesforce/model credentials reach containers only by `${VAR}` interpolation; there is no `env_file:` anywhere | `docker-compose.yml` (verified: `rg -n 'env_file'` → no match) |

The `.gitignore` comment at `.gitignore:4-8` is the most security-load-bearing prose in the repository,
and the mechanism it describes demonstrably works.

### Wrong

- **`SEC-04` — a plaintext credential backup is on disk.** `.env.bak-205921` (553 bytes, mode `0600`,
  dated 2026-07-28) sits in the working tree. It is correctly untracked
  (`git check-ignore -v` → `.gitignore:47`), so this is not a git leak — but it is a second copy of
  live credentials with no rotation, no expiry and no owner, and it will be picked up by any backup,
  `rsync`, container build context or archive that does not read `.gitignore`.
  `docker-compose.yml.bak-preperf` and `searxng/settings.yml.bak` are the same pattern for
  non-secret files. Delete it.
- **`SEC-06` — dead AWS Secrets Manager config is still solicited.** `.env.example:7-14` asks the
  operator for `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `SF_SECRET_NAME`, under a
  comment describing a subsystem that was removed. **Zero references exist anywhere in code**, and
  none of the four is forwarded to any container. The template is actively asking for long-lived cloud
  credentials that nothing will ever use — the worst kind of stale documentation.
- **The template is incomplete in the direction that matters.** `SF_CLIENT_SECRET` — the *only*
  variable `core/salesforce.py:117-122` can actually authenticate with, and the path `README.md:157-163`
  calls simplest and recommended — is **absent from `.env.example`**. So are `SF_API_VERSION`,
  `SF_LIVE_TIMEOUT`, `SF_DICTIONARY_PATH`, `WORKSPACE_DIR`, `LOCAL_USERNAME`, `CORS_ALLOW_ORIGINS`,
  `MOCK_MODE` and all five `PROFILE_*` vars.
- **18 of the 43 variables the template does document never reach any container**, because there is no
  `env_file:` and each service enumerates its own `environment:` block
  (`docker-compose.yml:219-267`, `:293-318`). Setting them has no effect. Among the casualties:
  `CHART_TRIGGER_MODE`, `CHART_FUNNEL_STAGE_ORDER`, `CORS_ALLOW_ORIGINS` and every `ARCHIVE_MAX_*` /
  `PROFILE_*` security limit — those all run on their hard-coded defaults in production.
- `SEARXNG_SECRET: ${SEARXNG_SECRET:-please-change-me}` (`docker-compose.yml:339`) has a
  publicly-known default.

---

## (e) Untrusted content and prompt injection (`SEC-05`)

The model is a confused deputy with real capabilities: it authors the SQL that runs against the
Salesforce warehouse (`engines/sql.py:200`), and its output is rendered as markdown in the operator's
browser. Any text that reaches the prompt from a non-user source is an injection vector.

**Every path by which non-user text reaches a model prompt:**

| # | Source | How it enters | Delimited? | Told to distrust? | Evidence |
|---|---|---|---|---|---|
| 1 | Fetched web page body + provider snippet | pasted raw into the `user` turn | No | No | `engines/search.py:374-378`, `:397` |
| 2 | Pasted-URL page text | `f"[{i}] {d['title']} ({d['url']})\n{body}"` concatenated into the `user` turn | No | No | `engines/url.py:63`, `:75` |
| 3 | **The same URL text, re-injected as a `system` message on every later turn** | `main.py:502-510` prepends it with role `system` | No | No | `main.py:498-510` |
| 4 | Cloned third-party repo — README, file tree, source chunks | overview prompt + 60 000-char Q&A context | No | No | `engines/repo.py:64-74`, `:80-89` |
| 5 | Salesforce record text from LanceDB | `f"[{rid}] ({obj}) {text}"` into the `user` turn | No | No | `engines/rag.py:105-112`, `:121` |
| 6 | DuckDB result rows in the narrative | JSON-serialised into the `user` turn | No | No | `engines/sql.py:255-256`, `:279` |
| 7 | Live SOQL rows | `describe_rows()` output into the `user` turn | No | No | `engines/sql.py:370` |
| 8 | **Uploaded PDF filename** | interpolated straight into the prompt header: `header = f'Document: {filename}\n\n'` | No | No | `engines/document.py:42` |
| 9 | PDF / document extracted text | `f"\n\nExtracted text:\n{text}"` | Labelled, not fenced | No | `engines/document.py:45-47` |
| 10 | Org dictionary hint | server-derived from `sf_dictionary` | n/a — trusted | n/a | `engines/sql.py:99-102` |

**Path 3 is the most serious**, because it converts a one-shot injection into a persistent one. A page
fetched once is stored, then re-injected as a **`system`** message on every subsequent turn
(`main.py:502-510`), and `recent_turns` **keeps every system message forever regardless of age, by
design**: `return system + turns[-n:]` (`engines/__init__.py:16-19`). Attacker-controlled text
therefore acquires system-role authority and unbounded persistence in a conversation. Path 8 is the
cheapest: a file named `report.pdf\n\nSYSTEM: ignore prior instructions` needs no network at all.

**Two paths get this right, and are the model for fixing the rest:**

- **Uploaded dataset profiles** are fenced and carry an explicit rule telling the model to treat the
  content as data — `engines/dataset.py:27-28`, `:40-45`, `:71`.
- **The chart-decision model call** is a genuinely tight boundary: only `p.to_prompt_dict()` aggregate
  metadata is serialised (`chart_pipeline.py:59`), and `chart_profile.ColumnProfile.to_prompt_dict`
  (`chart_profile.py:136-151`) emits only `name/kind/rows/non_null/distinct` plus `min/max/has_negative`
  or `max_label_len`. **No Salesforce cell value can reach that prompt**, enforced by construction.
  The user's own `question` is the only free text (`chart_pipeline.py:75`).

**What is missing across paths 1–9:** no instruction-stripping, no provenance tainting (nothing marks
a span as untrusted downstream), no delimiter convention, and no output-side check that the model's
SQL is consistent with the *user's* question rather than with text a page told it to run. The blast
radius is currently bounded by `sql_guard` plus the read-only DuckDB flags (§c) — i.e. by the same two
flags carrying `SEC-07`.

**The rendering sink compounds it.** `frontend/components/Markdown.tsx:57-73` overrides `pre`, `code`,
`table` and `a` — but **not `img`**. react-markdown 10.1.0 (`frontend/package.json:21`) therefore
renders `![](https://…)` through `defaultUrlTransform`, whose `safeProtocol` allows `https`, into a
live `<img src>`. `frontend/next.config.mjs:1-8` sets **no `Content-Security-Policy`**, so the browser
issues the request. A page that convinces the model to emit an image tag exfiltrates whatever the model
puts in the URL — a classic markdown-image exfiltration channel, fully open.

Two related observations, neither with an assigned ID: `core/citations.py:15` performs no validation
or percent-encoding of `record_id` and no scheme check on `base_url`, and `engines/report.py:205-208`
writes the result into report markdown as a live link; and `core/exports.py:80-81`/`:112` write cell
values verbatim, so both exporters are confirmed formula-injection sinks (an
`=cmd|'/c calc.exe'!A0` value produces an xlsx cell with `data_type == 'f'`).

---

## (f) STRIDE summary

| STRIDE | Threat, concretely | Control that exists | Status | Finding |
|---|---|---|---|---|
| **S**poofing | Any client on the network acts as the single local user | None. `require_user` never 401s (`auth.py:95-97`); `/chat` and `/reports*` are auth-free by design (`main.py:55-56`) | **Absent** | `SEC-01` |
| **S**poofing | Cross-origin browser page drives `/chat` | CORS allowlist of the two localhost:3000 origins (`main.py:47-53`, `config.py:244-250`) | **Effective for browsers only** — no defence against `curl` | `SEC-01` |
| **S**poofing | Attacker reaches another user's conversation by guessing its id | `db.conversation_owner` gate (`main.py:338-344`) | **Falls open** on any DB exception (`main.py:340-341`) | `SEC-02` |
| **T**ampering | Model-authored SQL writes to or attaches a database | `sql_guard` blocklist (`sql_guard.py:142-154`) + DuckDB `read_only=True` (`engines/sql.py:126`) | **Guard bypassable, DB flag holds** — `SELECT E'\'' , 1; DROP TABLE t` passes the guard and is stopped only by `InvalidInputException` | `SEC-07` |
| **T**ampering | Archive member escapes the extraction root (zip-slip, symlink) | `safe_member_name` + `resolves_inside` (two independent checks) + regular-file-only writes (`archive.py:96-112`, `:223`/`:266`, `:195`) | **Effective** | — |
| **T**ampering | `GET /reports/{filename}` traverses out of `REPORTS_DIR` | 6 syntactic rejects + `resolve()` + `is_relative_to` (`report_paths.py:28-47`) | **Effective** | — |
| **T**ampering | Injected instructions in fetched/cloned/retrieved text steer the model | None — 9 of 10 prompt paths pass raw text; only `dataset.py` fences and only `chart_pipeline.py` metadata-only | **Absent** | `SEC-05` |
| **R**epudiation | Attribute an action to a principal after the fact | Single shared local account; **no correlation/trace id propagates browser → orchestrator → engine → model** | **Absent** | `OBS-01` |
| **R**epudiation | Diagnose a silent degradation | 42 broad `except Exception` in `orchestrator/app`; `core/profile.py` and `core/sf_dictionary.py` contain **no logging at all** | **Absent** | `REL-03` |
| **I**nfo disclosure | Read any report or warehouse export over HTTP | Path resolver is sound; **no auth, no extension allowlist, no retention** (`main.py:257-271`, `engines/sql.py:407-409`) | **Absent** | `SEC-01` |
| **I**nfo disclosure | Read host files via a guard-approved SELECT | DuckDB `enable_external_access=False` (`engines/sql.py:127`) | **Effective — and it is the only layer**; the guard itself fails (`PermissionException` observed) | `SEC-07` |
| **I**nfo disclosure | SSRF to cloud metadata / private space | DNS pre-resolution + full private/reserved/IPv6/encoding coverage + per-hop redirect re-validation (`net.py:82-86`, `:147`) | **Effective except** rebinding (resolution not pinned) and CGNAT `100.64/10` | `SEC-03` |
| **I**nfo disclosure | Model exfiltrates data through a rendered markdown image | `Markdown.tsx:57-73` overrides `pre`/`code`/`table`/`a` but **not `img`**; `next.config.mjs:1-8` sets no CSP | **Absent** | `SEC-05` |
| **I**nfo disclosure | Credentials leak from disk or git | `.gitignore:10-17`/`:46-50` (verified effective); JWT key mounted as a file, not an env var (`docker-compose.yml:307-311,328`) | **Effective in git**; `.env.bak-205921` still on disk; dead AWS vars still solicited (`.env.example:7-14`) | `SEC-04`, `SEC-06` |
| **I**nfo disclosure | Salesforce secret leaks into a child process | None — `repo.py:165-172` passes `dict(os.environ)` to `git` | **Absent** | — (no ID assigned) |
| **D**enial of service | Unbounded request body (`messages`/`image`/`pdf`) | None — `ChatRequest` (`main.py:176-199`) declares no size bound and Starlette sets no body limit | **Absent** | `REL-01` |
| **D**enial of service | Unbounded PDF raster (up to ~3.3 GiB/page × 6 pages) | `MAX_PDF_PAGES=6` only; **no byte or pixel bound** (`core/pdf.py:37`, `:50`) | **Absent** | `REL-01` |
| **D**enial of service | Decompression bomb in an upload | Four independent caps incl. a live streaming budget (`archive.py:145`, `:166`, `:173`, `:200-208`) | **Effective per-request**; budget is **per-call, not global** (`archive.py:216`) | — |
| **D**enial of service | One request stalls every concurrent SSE stream | None. Single uvicorn process, no `--workers` (`Dockerfile:52`); blocking DuckDB in `async` (`engines/sql.py:201,206`), 180 s `git clone` on the loop (`repo.py:182`), 4.8 s xlsx export (`engines/sql.py:408`), 0.7 s matplotlib render (`report.py:190`) | **Absent** | `PERF-01` |
| **D**enial of service | Unbounded response body from a fetched host | `max_bytes` applied **after** the full body is buffered (`net.py:137` vs `:153`) | **Ineffective** | `PERF-02` |
| **D**enial of service | Unmetered GPU consumption on published model ports | None — no `--api-key` on any vLLM service | **Absent** | `COST-01` |
| **D**enial of service | Application layer does not come back after a host restart | `orchestrator`, `sync-worker` and `frontend` have **no `restart:` policy**, unlike all four model servers | **Absent** | `REL-02` |
| **E**levation of privilege | Cloned repository code executes | Code is treated strictly as data: `--depth 1 --no-tags --single-branch`, `core.hooksPath=/dev/null`, `credential.helper=`, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=/bin/true`, `GIT_CONFIG_NOSYSTEM=1`, and `.git/hooks` deleted after clone (`repo.py:165-194`) | **Effective** | — |
| **E**levation of privilege | Code-executing upload formats (`.pkl`, `.so`, `.xlsm`) are deserialised | `REFUSED_SUFFIXES` enforced at both `archive.py:126-131` and `profile.py:212-214` | **Effective** | — |
| **E**levation of privilege | A regression removes the only real SQL control | Nothing enforces the DuckDB flags — `engines/sql.py:124-132`, `schema_cache.py:40-48` and `core/profile.py:52-70` each configure a connection by hand, and `profile.py:61-69` wraps each pragma in `except Exception: pass` | **Absent** — no test asserts the flags, and there is **no CI at all** | `SEC-07`, `TEST-01` |

### Priority

1. **`SEC-01`** — put authentication in front of `/chat`, `/reports*` and the four vLLM ports, or bind
   every published port to `127.0.0.1`. Everything else in this document is bounded by whether an
   attacker can reach the socket.
2. **`SEC-02`** — make `main.py:338-344` fail closed.
3. **`SEC-03`** — pin the resolved IP in `net.safe_fetch`.
4. **`SEC-07`** — the guard is not the control; make the DuckDB flags the stated control, assert them
   in a test, and fix or replace `_scan`.
5. **`SEC-04`/`SEC-06`** — delete `.env.bak-205921`; delete `.env.example:7-14`; add
   `SF_CLIENT_SECRET`.
6. **`SEC-05`** — fence and taint the nine untrusted prompt paths, starting with the `system`-role
   re-injection at `main.py:502-510`, and override `img` in `Markdown.tsx` or ship a CSP.
