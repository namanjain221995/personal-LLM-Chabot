# Quick Wins — everything fixable in under a day

Ordered by impact ÷ effort. Every item here is **S effort (<4h)** and most are minutes.
Full context for each is in [`IMPROVEMENT-REPORT.md`](IMPROVEMENT-REPORT.md).

**Doing items 1–9 costs well under one engineer-day and removes the only P0 plus four P1s.**

---

## 1. Close the front door — `SEC-01` (P0) · ~10 minutes

The single highest-value change in this codebase. Six ports are published on `0.0.0.0` with no authentication.

```yaml
# docker-compose.yml
# orchestrator (:272-273)
ports: ["127.0.0.1:8080:8080"]
# frontend (:351-352)
ports: ["127.0.0.1:3000:3000"]
```

For the four vLLM services, **delete the `ports:` block entirely** — the orchestrator reaches them over the
compose network by service name (`http://vllm:30000`, `http://vllm-router:30002`, `http://vllm-embed:30003`),
so no host binding is needed at all:

```yaml
# docker-compose.yml:85-86 (vllm), :132-133 (vllm-vision), :170-171 (vllm-router), :201-202 (vllm-embed)
-    ports:
-      - "8000:30000"
```

**Verify — from a different machine, all six must refuse:**
```bash
for p in 3000 8000 8001 8002 8003 8080; do nc -z -w2 <host> $p && echo "STILL OPEN: $p"; done
curl -sf http://127.0.0.1:8080/health | jq .status   # on the host, still works
```

Also closes `COST-01` and removes the remote exploitability of `REL-01`.

---

## 2. Unblock the event loop — `PERF-01` (P1) · ~15 minutes

`_execute()` is a synchronous DuckDB call made directly inside `async def`. Four other modules
(`net.py:121`, `health.py:122`, `rag.py:98`, `search.py:326`) already do this correctly — `engines/sql.py`
was missed.

```python
# orchestrator/app/engines/sql.py — add `import asyncio` at the top, then:
# line 201
columns, rows = await asyncio.to_thread(_execute, sql, cap)
# line 206
columns, rows = await asyncio.to_thread(_execute, sql2, cap)
```

**Verify:** run a deliberately slow query and confirm a second concurrent `POST /chat` still streams tokens.

---

## 3. Stop silently wrong rankings — `DATA-04` interim (P1) · ~20 minutes

The proper fix (typing the warehouse) is M effort, but the damage — `ORDER BY Amount` returning the wrong
rows — can be stopped today by telling the model the truth about the column types. Add to `_SQL_SYSTEM` in
`orchestrator/app/engines/sql.py:39-69`, right after the existing checkbox rule at `:60-63`:

```python
"- Every column in this warehouse is stored as TEXT, including amounts, "
"counts and dates. NEVER order or compare them directly — '9000' sorts "
"above '10000'. Always cast first: ORDER BY TRY_CAST(Amount AS DOUBLE) DESC, "
"WHERE TRY_CAST(CloseDate AS DATE) >= DATE '2026-01-01', "
"SUM(TRY_CAST(Amount AS DOUBLE)).\n"
```

**Verify:** ask "what are our top 5 opportunities by amount?" and check the generated SQL contains `TRY_CAST`
and that the returned order is numerically correct.

---

## 4. Bound the request body — `REL-01` (P1) · ~30 minutes

```python
# orchestrator/app/main.py:171-199
from pydantic import Field

class ChatMessage(BaseModel):
    role: str
    content: str = Field("", max_length=100_000)

class ChatRequest(BaseModel):
    messages: Optional[List[ChatMessage]] = Field(None, max_length=200)
    image: Optional[str] = Field(None, max_length=15_000_000)
    image_base64: Optional[str] = Field(None, max_length=15_000_000)
    pdf: Optional[str] = Field(None, max_length=70_000_000)
```

**Verify:** a 300 MB body returns `422`/`413` instead of being parsed.

---

## 5. Add CI — `TEST-01` (P1) · ~45 minutes

1,141 tests already pass in under a minute; they just never run themselves. The ready-to-commit
`.github/workflows/ci.yml` is in [`IMPROVEMENT-REPORT.md` §4 TEST-01](IMPROVEMENT-REPORT.md).

**Verify:** open a PR with a broken assertion; the check must fail.

---

## 6. Fail closed on the ownership check — `SEC-02` (P1) · ~10 minutes

```python
# orchestrator/app/main.py:338-341
try:
    conv_owner = db.conversation_owner(conv_key_outer)
except Exception:
    log.exception("ownership check failed for %s", conv_key_outer)
    raise HTTPException(status_code=503, detail="temporarily unavailable")
```

Fix the comment above it too — it currently claims the code re-raises, which it does not.

---

## 7. Delete the plaintext credential backup — `SEC-04` (P2) · ~1 minute

```bash
shred -u .env.bak-205921        # or: rm -P
```

It holds real `HF_TOKEN`, `SF_CLIENT_ID` and `SF_PRIVATE_KEY` values. `.gitignore` already covers `*.bak-*`
and `git ls-files` confirms it was never committed — so this is host and backup-media exposure, not VCS
exposure. Delete it anyway; there is no reason for it to exist.

---

## 8. Strip the dead AWS config — `SEC-06` (P2) · ~2 minutes

`.env.example:7-14` still asks operators to supply `AWS_REGION`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `SF_SECRET_NAME`. All four have **zero** references anywhere in the codebase or
compose file — Secrets Manager was removed on 2026-07-28 (`syncworker/secrets.py:3`,
`docker-compose.yml:294`). Delete those lines so nobody puts live cloud credentials in a file for a feature
that no longer exists.

While there: `orchestrator/app/engines/__init__.py:24` (`NO_DATA_MESSAGE`) still tells the *user* the system
"needs the AWS credentials and region in `.env`" — fix that string too (`DOC-01`).

---

## 9. Guard the SQL retry path — `DATA-01` (P2) · ~10 minutes

`references_a_known_table()` runs on the first attempt (`engines/sql.py:193`) but not on the retry, so the
retry can execute the invented SQL the first attempt was refused for.

```python
# orchestrator/app/engines/sql.py:203-207
except Exception as exc:
    raw2 = await _ask_sql(question, schema_text, history, previous_sql=raw, error=str(exc))
    if not references_a_known_table(raw2, schema):
        raise NoSuchTable("the question refers to data that is not in the local warehouse")
    sql2 = guard_sql(raw2)
```

---

## 10. Index the warehouse `Id` columns — `DATA-02` (P2) · ~20 minutes

Tables are created by `CREATE TABLE AS SELECT` (`syncworker/storage.py:140`), so they carry no primary key
and no index. Every sync batch then runs `DELETE FROM "<obj>" WHERE Id IN (SELECT Id FROM _staging_df)`
(`:156`) as a full scan.

```python
# syncworker/storage.py, immediately after the CREATE TABLE at :140
con.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_id_idx" ON "{table}"(Id)')
```

---

## 11. Add a restart policy to sync-worker — `REL-02` (P2) · ~5 minutes

Every other service has one; this one does not, so if it dies the warehouse silently stops refreshing while
`/health` still reports ok.

```yaml
# docker-compose.yml:291-331
  sync-worker:
    build: ./sync-worker
    restart: unless-stopped
```

---

## 12. Reuse the OpenAI client — `PERF-04` (P2) · ~30 minutes

`llm.py:72-79` constructs a fresh `AsyncOpenAI` (and therefore a fresh `httpx` client and connection pool) on
**every** call, and never closes it. Build one per base URL at module scope and reuse.

---

## 13. Skip the pointless PDF rasterisation — `PERF-05` (P2) · ~30 minutes

`core/extract.py:55-61` calls `render_pdf(...)` purely to get the text layer and throws the rendered images
away into an unused `_images` variable. Add a text-only entry point to `core/pdf.py`.

---

## 14. Add `python-multipart` to `requirements-dev.txt` — `DX-03` (P2) · ~2 minutes

It is in `requirements.txt:5` but missing from `requirements-dev.txt`, which claims to be "identical …
EXCEPT no transformers, no weasyprint". It is needed at **import** time — `main.py:23` imports `.uploads`,
whose `File()`/`Form()` decorators raise `RuntimeError: Form data requires "python-multipart"`. On a clean host
the entire orchestrator suite fails at **collection**; it only works here because the machine happens to have it.

```diff
  pytest>=8.0
+ python-multipart>=0.0.9
```

**Fix this before adding CI** (item 5) — otherwise the new pipeline fails on its first run.

---

## 15. Run the orchestrator container as non-root — `SEC-11` (P2) · ~15 minutes

`orchestrator/Dockerfile` has **no `USER` directive**, while `sync-worker/Dockerfile:23` sets `USER worker` and
`frontend/Dockerfile:28` sets `USER nextjs`. The orchestrator is the container holding `/data` read-write —
including the Salesforce JWT signing key — *and* executing LLM-generated SQL. The inconsistency with the other
two images shows this is an oversight rather than a decision.

```dockerfile
RUN useradd --uid 10002 --create-home orchestrator \
 && chown -R orchestrator /data /reports
USER orchestrator
```

---

## 16. Fix the README/config drift — `DOC-02` (P3) · ~1 minute

`README.md:142` documents `SEARCH_MAX_RESULTS=10`; `.env.example:55` and `config.py:197` both say `100`.

---

## 17. Delete dead code — `QUAL-02` (P3) · ~2 minutes

`is_safe_select()` (`core/sql_guard.py:160-166`) is never called by application code. Remove it, or keep it
and note it is test-only — but do not leave an unused second entry point into a security guard.

---

### Not a quick win, but do it right after

`SEC-07` (the `sql_guard` escape-string bypass) and `SEC-03` (the SSRF rebinding window) are both **M effort**
and both matter. They are the top of the "Next" column in the roadmap for a reason — neither is currently
exploitable, but both defeat a control the system is documented as relying on.
