# Architecture Review & Technical Due Diligence — TechSara Local Salesforce AI Platform

**Audited:** 2026-07-31 · 251 files · 43,189 LOC · every in-scope file read in full
**Verdict:** A genuinely well-engineered application with **one deployment-level flaw that dominates its entire risk profile.**

---

## 1. Executive summary

*Written for a non-technical reader. Every claim below is backed by a file and line number later in this report.*

This is a good codebase. That is not a courtesy — the security-sensitive parts (archive handling, file-path
handling, outbound network fetching) are written to a standard above what we normally see, there are 1,141
automated tests and **all of them pass**, and there is not a single `TODO` or `FIXME` left in the source. The
team clearly knows what it is doing.

The problem is not the code. It is **where the code is plugged in.**

### The top 5 risks

| # | Risk | What happens if ignored | Cost to fix |
|---|---|---|---|
| **1** | **The application has no login, and every port is open to the whole network.** | Anyone who can reach this machine — any device on the office Wi-Fi, any guest, any compromised laptop — can read every conversation, query the copied **production Salesforce data**, and use the GPU for free. No password is required because there is no password. | **1 line of config. Under 1 hour.** |
| **2** | **Analytics numbers can be silently wrong.** Every Salesforce value is stored as text, so "top 5 deals by value" sorts `9000` above `10000`, and `MAX(Amount)` returns the wrong deal. Totals (`SUM`) fail loudly; rankings fail *silently*. | The product's entire purpose is answering questions about numbers. A confidently-presented wrong ranking is worse than an error, because nobody catches it. | 1–3 days |
| **3** | **One slow database query freezes the app for everyone.** The database call blocks the server's single event loop. | While one heavy report runs, every other user's answer stops mid-sentence. The team already solved this correctly in four other places — this one spot was missed. | **Under 4 hours** |
| **4** | **There is no automated build or test gate (no CI).** 1,141 good tests exist and are only run by hand. | The safety net only works when someone remembers to use it. A regression reaches `main` unnoticed. | 1 day |
| **5** | **Anyone can send an unlimited-size request and exhaust the machine's memory.** No size limit on uploads or message bodies. | Combined with risk #1, a single request from anywhere on the network can take the box down. | **Under 4 hours** |

### The headline

**Four of the five top risks are fixable in under two days combined.** Risk #1 — by far the most severe — is a
one-line change to a configuration file. The codebase does not need re-architecting; it needs its front door
closed.

The most striking finding of this audit is that the code *already knows*. `orchestrator/app/auth.py:17-20`
carries this comment:

> *"there is now no authentication whatsoever. Anyone who can reach the port can read every conversation and
> query the Salesforce data. That is fine for a machine only you can reach, and **NOT fine if the port is
> published to a network you do not control — see the compose port bindings**."*

We checked the compose port bindings. They publish to `0.0.0.0` — every network interface. **The condition the
code relies on to be safe is not met.**

---

## 2. Architecture scorecard

| Dimension | Score | Evidence |
|---|:---:|---|
| Architecture & modularity | **8**/10 | Clean engine/core/context separation; one guarded egress choke point (`core/net.py:103`); 21 focused core modules. Loses points for no engine ABC (`QUAL-01`) and two god-files (`db.py` 1064 LOC, `ChatApp.tsx` 916 LOC) |
| **Security (application logic)** | **8**/10 | `archive.py`, `report_paths.py` are exemplary; SSRF guard resolves DNS before connect and re-validates every redirect hop; CORS correctly scoped (`config.py:244-250`); nothing sensitive is git-tracked |
| **Security (deployment posture)** | **1**/10 | No authentication at all (`auth.py:95-97`) + 6 ports published on `0.0.0.0` (`docker-compose.yml:86,133,171,202,272-273,351-352`). `SEC-01` |
| Reliability | **5**/10 | Detached-generation design is excellent (`main.py:62-120`); sync is genuinely crash-safe. But: unbounded request bodies (`REL-01`), no restart policy on sync-worker (`REL-02`), 42 broad `except Exception` blocks |
| Performance & GPU efficiency | **5**/10 | GPU budget carefully reasoned (`docker-compose.yml:18-25`); rerank correctly off-loaded. But a blocking DuckDB call sits on the event loop (`PERF-01`) and the GPU ceiling is enforced only by a YAML comment |
| **Data integrity** | **4**/10 | Watermark ordering is crash-safe and idempotent — a real strength. Undone by everything being `VARCHAR` (`DATA-04`), no PK/index on `Id` (`DATA-02`), and 4 tables that orphan on delete (`DATA-03`) |
| Code quality | **9**/10 | **Zero** TODO/FIXME/HACK in 43k LOC; comments explain *why*, often citing the real incident that motivated the code; no dead modules |
| Test coverage & quality | **6**/10 | 1,141 tests, **0 failing**, 0.47 test-LOC per source-LOC. But all unit-level — none of the 8 critical paths has end-to-end coverage (`TEST-02`) |
| Observability | **3**/10 | No correlation/trace id anywhere (`OBS-01`); `/health` probes real dependencies (good) but several failures degrade silently |
| CI/CD & DX | **2**/10 | **No CI exists at all** (`TEST-01`); orchestrator deps unpinned with no lockfile (`DX-01`) |
| Documentation | **8**/10 | README + 858-line CHANGELOG + rich in-code rationale. Drift: dead AWS config still advertised (`SEC-06`), `MOCK_MODE` undocumented (`DX-02`) |
| Cost efficiency | **6**/10 | Sensible model right-sizing (35B main / 8B classifier / 0.6B embed). But unauthenticated GPU ports allow unmetered use (`COST-01`) |

**Weighted verdict: 5.4/10 as deployed · 7.6/10 as written.** That gap is the whole story, and it is cheap to close.

---

## 3. What is genuinely good

Called out specifically, because a review that only lists problems misrepresents this codebase.

1. **`core/archive.py` is the best module in the repository.** Hostile-input handling done properly: magic-byte
   sniffing so a renamed file cannot pick its reader (`:74-89`); a **manual member loop, never `extractall`**;
   zip-slip checked twice — once on the name (`:96-112`), once on the resolved realpath (`:115-123`); symlinks,
   hardlinks and devices refused (`:154-156`, `:251-253`); **four independent bomb caps** re-checked against a
   live streaming budget because "the header can lie" (`:192-209`); nested archives listed but never opened; and
   `.xlsx` deliberately routed through the same caps because *"an xlsx IS a zip"* (`:134-139`). That last detail
   is one most teams miss.

2. **`core/report_paths.py`** rejects `..`, separators, absolute paths, hidden files and NUL bytes, *then*
   verifies the resolved realpath stays inside the root — closing the symlink escape that name-checking alone
   misses (`:42-47`).

3. **The SSRF guard is a real choke point, not a checkbox.** `core/net.py` resolves DNS *before* connecting,
   rejects the host if **any** resolved address is private, follows redirects manually so every hop is
   re-validated (`:141-148`), and caps time and body size. It even off-loads the blocking `getaddrinfo` to a
   thread with a comment explaining that doing it inline *"froze the whole event loop… stalling SSE token
   delivery for every other user"* (`:117-121`).

4. **The Salesforce sync is genuinely crash-safe.** `syncworker/main.py:167` upserts before `:188` commits the
   watermark, and the upsert is `DELETE`-by-Id + `INSERT` inside one transaction (`storage.py:135-167`). Kill
   the process anywhere mid-cycle and the next run re-fetches and re-applies with no duplicates and no data
   loss. The code says so and the code is right.

5. **The detached-generation design (`main.py:62-120`)** lets an answer survive a browser reload, supports
   multiple attached readers, and uses a `generation_id` idempotency key enforced by a **unique database index**
   (`db.py:188`) rather than an application-level check that would lose the race. That is a considered design.

6. **Honest engineering comments.** Repeatedly the code records the actual incident that motivated it — the
   model answering *"0 records"* for an object that was never synced (`sql.py:158-167`); reporting a 29-row
   sample as the total of 314 (`sql.py:268-273`); claiming *"the live Salesforce check confirms"* from a cached
   copy (`sql.py:263-266`). Each has a guard written around it. This is a team that fixes root causes.

7. **Zero TODO/FIXME/HACK markers in 43,189 LOC**, and no dead modules — every module is imported or is a
   documented entrypoint.

8. **The web-search guard is well-reasoned:** in `salesforce` mode only an *explicit* toggle searches the web,
   because auto-detection once *"came back with web articles about IT ticketing instead of this org's cases"*
   (`main.py:394-401,430-433`).

---

## 4. Findings

Severity is calibrated against what the code **actually does** when deployed as configured. See
`../ASSUMPTIONS.md#a11` for the calibration rationale.

---

### [SEC-01] Bind every published port to loopback — the platform has no authentication and is exposed on all interfaces
- **Severity:** P0 Critical
- **Category:** Security
- **Evidence:** `orchestrator/app/auth.py:89-97`, `orchestrator/app/main.py:56`, `docker-compose.yml:86,133,171,202,272-273,351-352`
- **What's wrong:** `current_user()` discards its `Request` and unconditionally returns the single local account
  (`auth.py:89-92`); `require_user()` is a pass-through whose own docstring reads *"Never 401s now"*
  (`auth.py:95-97`). `main.py:56` records that `/chat` and `/reports*` are auth-free by design. Meanwhile six
  host ports are published with Docker's short syntax — `8080:8080`, `3000:3000`, `8000:30000`, `8001:30001`,
  `8002:30002`, `8003:30003` — which binds **`0.0.0.0`**, every interface. CORS is correctly restricted
  (`config.py:244-250`) but CORS constrains browsers, not `curl`.
- **Why it matters:** Any host that can route to this machine can, with no credential:
  `curl -N -X POST http://<host>:8080/chat -d '{"message":"list every opportunity with amounts and owners"}'`
  — reading **production CRM data** copied into the warehouse; `GET /history/conversations` to read every past
  conversation; `GET /reports` plus `GET /reports/{filename}` to download every generated export; and
  `POST http://<host>:8000/v1/chat/completions` to consume the GPU directly. The code's own security note
  (`auth.py:17-20`) states this posture is *"NOT fine if the port is published to a network you do not
  control — see the compose port bindings"*. The compose port bindings are exactly that.
- **Fix:** Bind to loopback. In `docker-compose.yml`, for each published service:
  ```yaml
  ports:
    - "127.0.0.1:8080:8080"   # orchestrator  (was "8080:8080")
    - "127.0.0.1:3000:3000"   # frontend
    - "127.0.0.1:8000:30000"  # vllm
    - "127.0.0.1:8001:30001"  # vllm-vision
    - "127.0.0.1:8002:30002"  # vllm-router
    - "127.0.0.1:8003:30003"  # vllm-embed
  ```
  The vLLM ports need no host binding at all — the orchestrator reaches them over the compose network — so
  deleting those four `ports:` blocks entirely is strictly better. If the UI must be reachable from other
  machines, put a reverse proxy with real authentication in front of `:3000` **only**.
- **Effort:** S (<4h) **Blast radius:** `docker-compose.yml` only; no application code changes.
- **Verification:**
  ```bash
  # from ANOTHER machine on the network — all six must refuse to connect:
  for p in 3000 8000 8001 8002 8003 8080; do nc -z -w2 <host> $p && echo "STILL OPEN: $p"; done
  # on the host itself, the app must still work:
  curl -sf http://127.0.0.1:8080/health | jq .status
  ```

---

### [SEC-07] Fix the `sql_guard` escape-string desynchronisation — a crafted SELECT defeats the multi-statement check
- **Severity:** P1 High
- **Category:** Security
- **Evidence:** `orchestrator/app/core/sql_guard.py:87-103`, `:142`
- **What's wrong:** `_scan()` models `''` as the only in-string quote escape. It does not model
  PostgreSQL/DuckDB **`E'…'` escape strings**, where `\'` also escapes a quote. On `E'\''` the scanner reads
  `\` as ordinary content, then treats the following `''` as an escaped quote and **stays inside the literal**,
  while DuckDB closes the literal at the same point. From there the two disagree about what is string content.
  Everything after lands in `cleaned` (which is executed) but is stripped from `bare` (which is scanned), so
  the keyword blocklist, the table-function blocklist **and the `;` multi-statement check at `:142` all see
  nothing**. Confirmed empirically against the real module — all four of these are returned as "safe":
  ```
  SELECT E'\'', * FROM read_csv('/etc/passwd')
  SELECT E'\'' , 1; DROP TABLE t              <-- passes despite the ';'
  SELECT 1 WHERE 'a' = E'\''||'x' AND read_blob('/etc/shadow') IS NOT NULL
  SELECT * FROM '/etc/passwd'                 <-- DuckDB replacement scan, no function name needed
  ```
- **Why it matters:** The module's docstring promises *"Accepts exactly ONE read-only statement"*. That promise
  is false, and `guard_sql` is the component the whole text-to-SQL feature is trusted on.
  **It is not currently exploitable** — and this must be stated plainly. Every payload above was executed
  against a DuckDB handle configured exactly as `engines/sql.py:124-132` configures it, and all four were
  refused: `PermissionException` for the file reads, `InvalidInputException: Cannot execute statement of type
  "DROP" … read-only` for the stacked statement. The defence-in-depth holds. But the entire safety argument now
  rests on two connection flags never regressing, and the stacked-statement test proves DuckDB *will* execute a
  second statement when permitted — only `read_only=True` stopped it.
- **Fix:** Teach `_scan` about escape strings, and stop trusting a regex for this. Minimum fix — treat a
  backslash inside a literal as escaping the next character when the literal was introduced by `E'`:
  ```python
  # in _scan, at the start of the ' branch:
  is_escape_string = i >= 1 and sql[i-1] in ("e", "E") and (i < 2 or not sql[i-2].isalnum())
  ...
  # inside the literal loop, before the quote checks:
  if is_escape_string and sql[i] == "\\" and i + 1 < n:
      cleaned.append(sql[i:i+2]); i += 2; continue
  ```
  Also add `E'` handling for `$$`-quoted strings, and reject any statement containing a `;` in **`cleaned`** as
  well as in `bare` — a cheap independent check that no scanner desync can bypass:
  ```python
  if ";" in cleaned.strip().rstrip(";"):
      raise SQLGuardError("multiple SQL statements are not allowed")
  ```
  Strategically, replace the hand-rolled scanner with `sqlglot.parse` (already a transitive dep of nothing here,
  but small) and assert the AST is exactly one `Select`.
- **Effort:** M (1-3d) **Blast radius:** `core/sql_guard.py`, `tests/test_sql_guard.py`.
- **Verification:** add the four payloads above to `orchestrator/tests/test_sql_guard.py` as
  `pytest.mark.parametrize` cases asserting `SQLGuardError` is raised; keep the existing 146 lines of tests green.

---

### [DATA-04] Stop storing every Salesforce value as text — rankings and aggregates are silently wrong
- **Severity:** P1 High
- **Category:** Data
- **Evidence:** `sync-worker/syncworker/storage.py:38-56`, `:140`, applied at `sync-worker/syncworker/main.py:164-165`
- **What's wrong:** `normalize_records()` coerces **every** value to `str` or `None` (`storage.py:47-54`). The
  resulting DataFrame is written with `CREATE TABLE "<obj>" AS SELECT * FROM _staging_df` (`storage.py:140`), so
  every column in every Salesforce object table lands as **`VARCHAR`** — amounts, dates, counts, probabilities.
  The stated rationale (keeping types stable between Bulk CSV and REST JSON syncs) is legitimate; the
  consequence was not followed through.
- **Why it matters:** Verified against DuckDB 1.5.4 with a table shaped exactly as the sync produces it:
  | Query | Result | Correct |
  |---|---|---|
  | `ORDER BY Amount DESC` over 9000 / 10000 / 800 | `9000, 800, 10000` | `10000, 9000, 800` |
  | `MAX(Amount)` | `'9000'` | `10000` |
  | `SUM(Amount)` | **Binder Error — query fails** | `19800` |

  `SUM` fails *loudly* and is caught by the one-shot retry, so it usually self-corrects. **`ORDER BY` and `MAX`
  fail silently.** "Show me our top 5 opportunities by value" returns the wrong five deals, formatted
  confidently, with a citation. For a product whose entire purpose is answering questions about numbers, this is
  the most consequential correctness defect in the system — and it is invisible.
- **Fix:** Preserve types at write time. `config.yaml` already declares each object's fields, so the type is
  known. In `storage.py`, stop blanket-stringifying and cast per declared Salesforce type:
  ```python
  _SF_TO_PANDAS = {"currency": "Float64", "double": "Float64", "int": "Int64",
                   "percent": "Float64", "date": "datetime64[ns]",
                   "datetime": "datetime64[ns]", "boolean": "boolean"}
  # keep str for id/reference/picklist/textarea/string — those genuinely are text
  ```
  and apply it to the DataFrame before `upsert()`. Note `storage.py:151-154` already handles schema drift with
  `ALTER TABLE ADD COLUMN`, so mixed-type history needs a one-off rebuild: because the sync is idempotent
  (see §3.4), the safe migration is to drop the object tables, reset the watermarks in `_sync_meta`, and let a
  full re-sync repopulate with correct types.
  **Interim mitigation (ship today, S effort):** add to `_SQL_SYSTEM` in `engines/sql.py:39-69` an explicit
  rule that numeric and date columns are stored as TEXT and must be cast — `ORDER BY TRY_CAST(Amount AS DOUBLE)
  DESC` — mirroring the existing checkbox rule at `:60-63`.
- **Effort:** M (1-3d), or S for the prompt-level interim mitigation.
  **Blast radius:** `syncworker/storage.py`, `syncworker/main.py`, a full re-sync, `engines/sql.py` prompt.
- **Verification:**
  ```bash
  # after re-sync, types must not all be VARCHAR:
  duckdb /data/warehouse.duckdb -c "DESCRIBE Opportunity" | grep -i amount   # expect DOUBLE/DECIMAL
  duckdb /data/warehouse.duckdb -c "SELECT Name, Amount FROM Opportunity ORDER BY Amount DESC LIMIT 5"
  ```
  plus a `sync-worker/tests/test_upsert.py` case asserting a numeric column round-trips as a numeric dtype.

---

### [PERF-01] Off-load the blocking DuckDB query — one slow SQL call stalls every user's stream
- **Severity:** P1 High
- **Category:** Performance
- **Evidence:** `orchestrator/app/engines/sql.py:117-139` (`_execute`), called at `:201` and `:206` from `async def generate_and_run_sql`
- **What's wrong:** `_execute()` is a synchronous function that opens DuckDB, runs the query and fetches rows.
  It is called **directly inside an `async def`** with no `asyncio.to_thread`. For the duration of the query the
  event loop cannot run any other task.
- **Why it matters:** This is a single-process `uvicorn` app serving SSE streams. A 20-second aggregate over a
  large Opportunity table freezes **every** concurrent user's token stream for 20 seconds — answers visibly
  stop mid-sentence — and also stalls `/health`, making the container look unhealthy. The team already knows
  this pattern: `core/net.py:117-121` off-loads `getaddrinfo` with a comment explaining it *"froze the whole
  event loop for seconds… stalling SSE token delivery for every other user"*, and `health.py:122-123`,
  `engines/rag.py:98` and `engines/search.py:326` all off-load correctly. `engines/sql.py` is the one that was
  missed.
- **Fix:** two lines.
  ```python
  # orchestrator/app/engines/sql.py:201 and :206
  columns, rows = await asyncio.to_thread(_execute, sql, cap)
  ```
  plus `import asyncio` at the top. DuckDB connections are created and closed inside `_execute`, so it is
  already thread-confined and safe to move.
- **Effort:** S (<4h) **Blast radius:** `engines/sql.py`; `engines/report.py` reuses `generate_and_run_sql` and
  benefits automatically.
- **Verification:** with a deliberately slow query running (`SELECT count(*) FROM huge a, huge b`), a second
  concurrent `POST /chat` must still stream tokens, and `curl /health` must answer within its 2s probe budget.

---

### [SEC-02] Fail closed on the conversation-ownership check
- **Severity:** P1 High
- **Category:** Security
- **Evidence:** `orchestrator/app/main.py:338-344`
- **What's wrong:**
  ```python
  try:
      conv_owner = db.conversation_owner(conv_key_outer)
  except Exception:
      conv_owner = None                      # <-- fails OPEN
  viewer = int(signed_in["id"]) if signed_in is not None else None
  if conv_owner is not None and conv_owner != viewer:
      raise HTTPException(status_code=404, ...)
  ```
  Any exception makes `conv_owner` `None`, which **skips the ownership comparison entirely**. The comment
  immediately above claims the opposite — *"If the DB is unreachable this raises; the stores it guards are read
  through the same connection, so they fail too and nothing can leak"* — but the code catches that raise.
- **Why it matters:** The check exists specifically to stop someone who guesses a conversation id from pulling
  another account's fetched pages and indexed source code into their own prompt (the comment at `:331-335`
  says so). A transient SQLite lock — entirely plausible under WAL with a concurrent sync — silently disables it.
  Impact is bounded today because `SEC-01` means there is only one account anyway; it becomes a real IDOR the
  moment multi-user is restored, and the misleading comment guarantees the next reader trusts it.
- **Fix:** fail closed, and log.
  ```python
  try:
      conv_owner = db.conversation_owner(conv_key_outer)
  except Exception:
      log.exception("ownership check failed for %s", conv_key_outer)
      raise HTTPException(status_code=503, detail="temporarily unavailable")
  ```
  and correct the comment.
- **Effort:** S (<4h) **Blast radius:** `main.py` single call site.
- **Verification:** test that monkeypatches `db.conversation_owner` to raise and asserts `POST /chat` returns
  503 — not a successful generation.

---

### [SEC-03] Close the SSRF resolve-then-connect window (DNS rebinding)
- **Severity:** P1 High
- **Category:** Security
- **Evidence:** `orchestrator/app/core/net.py:58-87` (`resolve_public_ips`), `:90-100`, used at `:121` and `:147`, connect at `:137`
- **What's wrong:** `assert_url_is_fetchable()` resolves the hostname and rejects private addresses, then
  `httpx` performs its **own, independent** DNS resolution when it connects at `:137`. Between the check and the
  connect the answer can change. The docstring at `:61-63` argues that rejecting when *any* returned address is
  private *"defeats DNS-rebinding-style tricks"* — that is true only for the multi-A-record variant, where both
  addresses arrive in one response. The classic sequential attack returns a public IP to the first query and a
  private one (`127.0.0.1`, `169.254.169.254`) to the second, with a 0-second TTL.
- **Why it matters:** A user pastes a link, or a fetched page redirects to, `http://rebind.attacker.test/`. The
  guard resolves it to a public IP and allows it; `httpx` re-resolves and connects to `127.0.0.1:8080` — the
  orchestrator's own unauthenticated API — or to any internal service. This is the one gap in an otherwise
  strong SSRF implementation.
- **Fix:** pin the address that was validated, so the connection cannot use a different one.
  ```python
  ips = await asyncio.to_thread(resolve_public_ips, host)   # already returns the validated set
  transport = httpx.AsyncHTTPTransport(local_address=None)
  # connect to the vetted IP, preserve the Host header and SNI:
  pinned = str(httpx.URL(current).copy_with(host=ips[0]))
  resp = await client.get(pinned, headers={**headers, "Host": host}, extra={"sni_hostname": host})
  ```
  The clean implementation is a small custom `AsyncHTTPTransport` whose resolver returns only the vetted IPs.
  Apply it on the initial request **and** on every redirect hop (`:147`).
- **Effort:** M (1-3d) **Blast radius:** `core/net.py`; consumers (`engines/search.py`, `engines/url.py`,
  `engines/repo.py`) are unchanged. TLS certificate validation must still be against the **hostname**, not the IP.
- **Verification:** extend `orchestrator/tests/test_net_ssrf.py` with a stub resolver that returns a public IP
  on call 1 and `127.0.0.1` on call 2, and assert `UnsafeURLError`.

---

### [REL-01] Bound the request body — `/chat` accepts unlimited input
- **Severity:** P1 High
- **Category:** Reliability
- **Evidence:** `orchestrator/app/main.py:171-199` (`ChatMessage`, `ChatRequest`)
- **What's wrong:** `messages: Optional[List[ChatMessage]]` has no length bound; `content: str` has no
  `max_length`; `image`, `image_base64` and `pdf` are unbounded base64 strings. Starlette/FastAPI impose no
  default request-body limit, and `uvicorn` does not either. `UPLOAD_MAX_MB=200` guards the *multipart upload*
  route (`config.py:172`) but not the JSON chat body.
- **Why it matters:** `curl -X POST http://<host>:8080/chat -d @2gb.json` is buffered and parsed into Python
  objects before any handler runs. With `SEC-01` this is an unauthenticated remote OOM against a machine also
  holding the model weights. Even benignly, a user pasting a very large PDF as base64 can exhaust memory.
- **Fix:** bound the model, and add a body-size guard.
  ```python
  from pydantic import Field
  class ChatMessage(BaseModel):
      role: str
      content: str = Field("", max_length=100_000)
  class ChatRequest(BaseModel):
      messages: Optional[List[ChatMessage]] = Field(None, max_length=200)
      image: Optional[str] = Field(None, max_length=15_000_000)   # ~11 MB decoded
      pdf: Optional[str] = Field(None, max_length=70_000_000)     # ~50 MB decoded
  ```
  plus a middleware rejecting `Content-Length` over a hard ceiling with `413`.
- **Effort:** S (<4h) **Blast radius:** `main.py` request models; the frontend already sends far smaller bodies.
- **Verification:** `POST /chat` with a 300 MB body returns `413`, and the process RSS does not spike.

---

### [TEST-01] Add CI — 1,141 passing tests are only run by hand
- **Severity:** P1 High
- **Category:** Testing
- **Evidence:** no `.github/`, no `.gitlab-ci.yml`, no `Jenkinsfile`, no `.circleci` anywhere in the repository (verified)
- **What's wrong:** There is no automated build, test or lint gate of any kind. Verified by running them
  manually: orchestrator **800 passed in 41.6s**, sync-worker **104 passed in 1.1s**, frontend **237 passed
  across 16 files** — 1,141 passing, 0 failing. All of it is opt-in.
- **Why it matters:** A well-maintained safety net that only fires when someone remembers to pull it is not a
  safety net. The whole suite runs in **under a minute** — the cost of automating it is trivial and the current
  state wastes the investment already made.
- **Fix:** commit this as `.github/workflows/ci.yml`:
  ```yaml
  name: CI
  on: [push, pull_request]
  jobs:
    orchestrator:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: '3.12' }
        # python-multipart is NOT in requirements-dev.txt (DX-03) but is needed at
        # IMPORT time — main.py:23 imports .uploads, whose File()/Form() decorators
        # raise RuntimeError without it. Without this the suite fails at collection.
        - run: pip install -r orchestrator/requirements-dev.txt python-multipart
        - run: cd orchestrator && python -m pytest -q
    sync-worker:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-python@v5
          with: { python-version: '3.12' }
        - run: pip install -r sync-worker/requirements.txt -r sync-worker/requirements-dev.txt
        - run: cd sync-worker && python -m pytest -q
    frontend:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: actions/setup-node@v4
          with: { node-version: '20', cache: 'npm', cache-dependency-path: frontend/package-lock.json }
        - run: cd frontend && npm ci
        - run: cd frontend && npx tsc --noEmit
        - run: cd frontend && npm run lint
        - run: cd frontend && npm test
  ```
  Note `torch` must not be installed in CI (`requirements.txt:2` — it comes from the GPU base image); the suite
  passes without it because the reranker is imported lazily. `python-multipart` **must** be added — see `DX-03`.
- **Effort:** S (<4h) **Blast radius:** one new file.
- **Verification:** open a PR with a deliberately broken assertion and confirm the check fails.

---

### P2 findings (condensed)

| ID | Title | Evidence | Fix | Effort |
|---|---|---|---|---|
| **SEC-04** | Delete the plaintext credential backup `.env.bak-205921` from the working tree | repo root; contains `HF_TOKEN`, `SF_CLIENT_ID`, `SF_PRIVATE_KEY` names | `shred -u .env.bak-205921`; `.gitignore` already covers it (`*.bak-*`) and `git ls-files` confirms it is untracked — the risk is host/backup exposure, not VCS | S |
| **SEC-05** | Prompt injection: untrusted web/URL/repo/document text enters the prompt unfenced | `engines/search.py:374-399`, `engines/url.py:53`, `db.py` url_documents | Wrap retrieved content in an explicit `<untrusted_source>` delimiter and add a system rule that content inside is data, never instructions; keep provenance on citations | M |
| **SEC-06** | Remove dead AWS Secrets Manager config from `.env.example` | `.env.example:7-14` — `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `SF_SECRET_NAME` have **0** references anywhere in code or compose; removal recorded at `syncworker/secrets.py:3` and `docker-compose.yml:294` | Delete those 4 entries; they invite real cloud credentials into a file for a deleted feature | S |
| **SEC-08** | `guard_soql` has no object/field allowlist | `core/salesforce.py:55-80` — checks single-statement, `SELECT`-prefix, forbidden keywords, forces a `LIMIT`; no scope restriction | Add an allowlist from `sync-worker/config.yaml`'s configured objects. Bounded today by the read-only integration user | M |
| **PERF-02** | `safe_fetch` buffers the entire body before applying `max_bytes` | `core/net.py:153` — `resp.content` is already fully downloaded before slicing | Switch to `client.stream()` and abort once the running total exceeds `max_bytes` | S |
| **PERF-03** | `db.connect()` re-runs the full schema + `migrate()` on every call | `db.py:195-205`, **32 call sites** in `db.py`; each runs 13 `CREATE TABLE IF NOT EXISTS` + 2 `PRAGMA table_info` | Run `executescript`/`migrate` once at startup (the lifespan hook at `main.py:27-38` already exists for this); keep a connection factory that only opens and sets pragmas | M |
| **PERF-04** | A new `AsyncOpenAI` client per LLM call, never closed | `llm.py:72-79`, constructed fresh in every helper | Build one module-level client per base_url and reuse; close on shutdown | S |
| **PERF-05** | PDF text extraction rasterises up to 10 pages and discards the images | `core/extract.py:55-61` — `_images, text, _total = render_pdf(...)`, `_images` unused | Add a text-only path to `core/pdf.py` that skips rendering | S |
| **DATA-01** | The SQL retry skips the hallucination guard | `engines/sql.py:203-207` — `references_a_known_table()` runs on attempt 1 (`:193`) but not on the retry | Move the check inside the retry branch too | S |
| **DATA-02** | No PK or index on `Id` in any warehouse table | `syncworker/storage.py:140` (`CREATE TABLE AS SELECT`), `:156` (`DELETE … WHERE Id IN …`) | `CREATE UNIQUE INDEX IF NOT EXISTS <obj>_id ON "<obj>"(Id)` after create | S |
| **DATA-03** | Four tables orphan permanently when a conversation is deleted | `db.py` — `uploads`, `url_documents`, `repos`, `repo_chunks` declare no FK, while `messages`/`conversation_summaries`/`conversation_chunks` do; `delete_conversation` (`db.py:334-340`) deletes only from `conversations` | Add `REFERENCES conversations(id) ON DELETE CASCADE` (a new-DB change; for existing DBs delete explicitly in `delete_conversation`). `PRAGMA foreign_keys=ON` is already set (`db.py:202`) | M |
| **REL-02** | sync-worker has no restart policy and no healthcheck | `docker-compose.yml:291-331` — every other service has both | Add `restart: unless-stopped` and a healthcheck asserting watermark freshness | S |
| **REL-03** | 42 broad `except Exception` handlers; several degrade silently | e.g. `main.py:161` (`contextlib.suppress` drops a lost answer), `:472`, `:494`, `:528`; `engines/router.py:121-122` | Log at `warning` with context in every swallow; keep the fallback behaviour | M |
| **OBS-01** | No correlation/trace id across browser → orchestrator → engine → model | no request-id middleware in `main.py`; `generation_id` exists but is not a log key | Generate a request id in the Next proxy, propagate as a header, bind it into structured logs and echo it on `meta` | M |
| **DX-01** | Orchestrator dependencies unpinned with no lockfile | `orchestrator/requirements.txt` — all `>=`, no upper bounds, no lock; contrast `sync-worker/requirements.txt` (majors capped) and `frontend/package-lock.json` | Adopt `pip-compile`/`uv` and commit `requirements.lock` | M |
| **TEST-02** | No end-to-end coverage for any of the 8 critical paths | 1,141 tests, all unit-level; frontend suite executes in 137 ms | See `../01-codebase/test-map.md` for the 10 highest-value tests to add | L |
| **COST-01** | Unauthenticated published vLLM ports permit unmetered GPU use | `docker-compose.yml:86,133,171,202` | Resolved by `SEC-01` — remove the host port bindings | S |
| **SEC-10** | Raw exception text is streamed to the browser as the terminal `error` event | `main.py:672` — `await gen.publish("error", {"message": str(exc)})` | Send a generic message to the client and log the detail server-side with the correlation id | S |
| **REL-04** | The router fallback chain has no per-call timeout | `engines/router.py:113-133` — both calls inherit `LLM_REQUEST_TIMEOUT=300.0` (`config.py:264`) and both `except Exception: pass` | A wedged classifier burns up to 600 s — plus `orchestrate.decide()`'s own 300 s — before defaulting to `"rag"`, with no user feedback. Wrap each classification call in `asyncio.wait_for(..., timeout=15)` | M |
| **SEC-11** | The orchestrator container runs as **root** | `orchestrator/Dockerfile` — **no `USER` directive** (0 matches), while `sync-worker/Dockerfile:23` sets `USER worker` and `frontend/Dockerfile:28` sets `USER nextjs` | It is the container holding `/data` read-write (including the Salesforce JWT signing key) *and* executing LLM-generated SQL. Add a non-root `USER`; the inconsistency with the other two images shows this is an oversight, not a decision | S |
| **DX-03** | `python-multipart` missing from `requirements-dev.txt` | `orchestrator/requirements-dev.txt` — present in `requirements.txt:5`, absent from dev, though the file claims to be "identical … EXCEPT no transformers, no weasyprint" | Needed at **import** time: `main.py:23` imports `.uploads`, whose `File()`/`Form()` decorators raise `RuntimeError: Form data requires "python-multipart"`. On a clean host the orchestrator suite fails at **collection**. Latent only because this machine happens to have it. Add it — and to CI | S |
| **DX-04** | 18 of 43 documented env vars reach no container | `docker-compose.yml` — **no `env_file:` anywhere** (0 matches); only explicitly interpolated `${VAR}` values are passed | Setting them in `.env` is a silent no-op — including every context-management threshold and `CHART_TRIGGER_MODE`. Config that reads as config but is inert. Either add `env_file: .env` to the orchestrator service or delete the dead entries | M |

### P3 findings (condensed)

| ID | Title | Evidence |
|---|---|---|
| **DX-02** | `MOCK_MODE` silently serves fabricated fixture answers and is undocumented in `.env.example` | `frontend/app/api/chat/route.ts:134`, and 6 other routes |
| **QUAL-01** | No engine ABC/Protocol; `Emit` re-declared independently | `engines/chat.py:22`, `engines/agent.py:34`, `graph.py:13` |
| **QUAL-02** | `is_safe_select()` is dead code — never called by application code | `core/sql_guard.py:160-166` |
| **QUAL-03** | Two god-files: `db.py` 1064 LOC, `ChatApp.tsx` 916 LOC | — |
| **SEC-09** | `archive.py` docstring claims the zip-slip check is re-run "at write time"; it runs once per member at `:223`, not inside `_write_member` | `core/archive.py:118-119` vs `:212-230` |
| **DOC-01** | Docs drift: `NO_DATA_MESSAGE` tells users to add "AWS credentials and region in `.env`" for a removed feature | `engines/__init__.py:24` |
| **DOC-02** | `README.md` documents `SEARCH_MAX_RESULTS=10`; `.env.example:55` and `config.py:197` both say `100` | `README.md:142` |
| **TEST-03** | Four tests that cannot fail: `assert True` (`test_search_off.py:29`), `assert settings.search_enabled in (True, False)` (`test_search_engine.py:115`), `assert … is None or True` (`test_chart_pipeline.py:199`), and a checker monkeypatched to itself (`test_conversation_integrity.py:553-566`) | as listed |

---

## 5. Prioritised roadmap

### Now — ≤ 2 weeks (dependency-ordered)

| # | Item | ID | Effort | Risk reduction |
|---|---|---|---|---|
| 1 | **Bind published ports to loopback / drop the vLLM host ports** | SEC-01 | S | **Eliminates the single P0.** Also resolves COST-01 and the exploitability of REL-01 |
| 2 | Off-load the blocking DuckDB call | PERF-01 | S | Removes cross-user stream stalls |
| 3 | Bound the request body | REL-01 | S | Removes the OOM vector |
| 4 | Add the CI workflow | TEST-01 | S | Makes every later fix verifiable |
| 5 | Interim: teach the SQL prompt to cast numerics | DATA-04 (part) | S | Stops silently wrong rankings *today* |
| 6 | Fail closed on the ownership check | SEC-02 | S | Removes a fail-open auth path |
| 7 | Delete `.env.bak-205921`; strip dead AWS vars | SEC-04, SEC-06 | S | Removes plaintext credentials from disk |

*Items 1–7 total well under one engineer-week and remove the P0 and three P1s.*

### Next — 30–60 days

| # | Item | ID | Effort | Depends on |
|---|---|---|---|---|
| 8 | Fix the `sql_guard` E-string desync (+ `;` check on `cleaned`) | SEC-07 | M | 4 |
| 9 | Pin the SSRF-validated IP at connect time | SEC-03 | M | 4 |
| 10 | Type the warehouse properly + full re-sync | DATA-04 | M | 4, 5 |
| 11 | Index `Id`; add FKs / explicit cleanup | DATA-02, DATA-03 | M | 10 |
| 12 | Stream-and-cap fetch bodies; reuse the OpenAI client; schema-once at startup; text-only PDF path | PERF-02/03/04/05 | M | 4 |
| 13 | Fence untrusted content in prompts | SEC-05 | M | — |
| 13a | Per-call timeouts on the router chain; sanitise the terminal error event | REL-04, SEC-10 | M | 4 |
| 14 | Correlation IDs + structured logging; log every swallow | OBS-01, REL-03 | M | 4 |
| 15 | Lockfile + pinned deps for the orchestrator | DX-01 | M | 4 |
| 16 | `restart: unless-stopped` + healthcheck for sync-worker | REL-02 | S | — |

### Later — 60–90 days

| # | Item | ID | Effort |
|---|---|---|---|
| 17 | End-to-end tests for all 8 critical paths (see `test-map.md`) | TEST-02 | L |
| 18 | SOQL object allowlist | SEC-08 | M |
| 19 | Enforce the GPU budget at runtime rather than by YAML comment | — | M |
| 20 | Split `db.py` and `ChatApp.tsx`; introduce a real engine `Protocol` | QUAL-01, QUAL-03 | L |
| 21 | Doc drift sweep; document `MOCK_MODE` or gate it to development | DX-02, DOC-01 | S |

---

## 6. Severity distribution

| Severity | Count |
|---|---:|
| **P0 Critical** | **1** |
| **P1 High** | **7** |
| **P2 Medium** | **22** |
| **P3 Low** | **8** |
| **Total** | **38** |

Consolidated from **162 raw candidate findings** produced by 12 independent subsystem readers, after
de-duplication (five readers independently reported the same auth/port exposure) and adversarial verification.
Several plausible findings were **investigated and dropped** because the code proved correct — model-output XSS,
the DuckDB replacement-scan "bypass", sync watermark data loss, and an unbounded generation registry. Those are
documented in `../ASSUMPTIONS.md#a12` so their absence reads as verified rather than missed.

---

## 7. The three things to fix this week

1. **Change six lines in `docker-compose.yml`** so the ports bind `127.0.0.1` instead of `0.0.0.0` — or delete
   the four vLLM `ports:` blocks entirely. This removes the only P0 and is the highest
   security-return-per-minute change available anywhere in this codebase.
2. **Add `await asyncio.to_thread(...)` around two calls in `engines/sql.py`.** Two lines; removes cross-user
   stream stalls.
3. **Commit the CI workflow.** The 1,141 tests already pass in under a minute — make them run themselves.
