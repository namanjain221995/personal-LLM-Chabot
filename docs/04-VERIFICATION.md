# Verification

Every self-check the brief requires, with the **actual output** produced when it was run. Anything that failed
was fixed and re-run; the failures and their fixes are recorded here rather than hidden.

Run date: 2026-07-31 · Repo: `/home/techsphere/Documents/projects/saleforce-LLM`

---

## ✅ 1. Every non-excluded source file appears in `00-INVENTORY.md`

```
find (in-scope):        251
inventory rows matched: 251
missing:                  0
```

Checked by iterating the authoritative file list and grepping each path out of the inventory:

```bash
while IFS=$'\t' read -r p lang loc; do
  grep -qF "$p" docs/00-INVENTORY.md || echo "MISSING: $p"
done < inv.tsv
# → no output
```

**PASS — counts match exactly.** Scope and exclusions are justified in [`ASSUMPTIONS.md#a1`](ASSUMPTIONS.md).

---

## ✅ 2. Every module doc has all 10 required sections; zero `TODO` placeholders

| Document | Purpose | Public | Control | State | Deps | Config | Failure | Concur | Cplx | Findings |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `orchestrator-engines.md` | 15 | 15 | 17 | 15 | 15 | 15 | 15 | 15 | 15 | 15 |
| `orchestrator-core.md` | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 |
| `orchestrator-context.md` | 16 | 16 | 16 | 16 | 16 | 16 | 16 | 16 | 16 | 16 |
| `orchestrator-search.md` | 6 | 6 | 6 | 6 | 6 | 6 | 6 | 6 | 6 | 6 |
| `frontend.md` | 45 | 45 | 44 | 44 | 45 | 45 | 45 | 45 | 45 | 45 |
| `sync-worker.md` | 12 | 12 | 12 | 12 | 12 | 12 | 12 | 12 | 12 | 12 |

**115 module blocks, all ten headings present.** (`orchestrator-engines.md` carries two extra *Control flow*
walkthroughs for the branching agent loop; `frontend.md` groups one pair of trivial presentational components
under a shared block — both are additive, not missing sections.)

`13/13` documents exist in `01-codebase/`:
`README · CRITICAL-PATHS · frontend · frontend-api-contracts · orchestrator-core · orchestrator-engines ·
orchestrator-context · orchestrator-search · sync-worker · infra-docker-compose · data-model · security-model ·
test-map`

**Zero `TODO` placeholders.** Every `TODO` string in `docs/` is a factual reference to the codebase containing
*none* — verified:

```bash
grep -rn "TODO" docs --include="*.md" | grep -v "TODO/FIXME" | grep -v "zero \`TODO\`"
# → only sentences of the form "No TODO/FIXME/HACK markers exist"
```

---

## ✅ 3. All 24 `.puml` exist, pass `-checkonly`, and render

```
src:  24 .puml
svg:  24
png:  24
-checkonly: 24/24 clean
```

> ⚠️ **`plantuml -checkonly` exits 0 even when a diagram fails to parse.** Verified directly: a file with a bad
> `!include` printed `Error line 22` and still returned `$? = 0`. Correctness is therefore judged by grepping
> the command's **output** for `error`, never by the exit code. `render.sh` implements it this way.

Rendering succeeded — **no `RENDER-SKIPPED`**. Toolchain per [`ASSUMPTIONS.md#a3`](ASSUMPTIONS.md):
`plantuml.jar` 1.2024.7 driven by the system JRE, with PlantUML's built-in **Smetana** layout engine
(`-Playout=smetana`) because `graphviz` is absent. The flag is passed on the command line only, never written
into the sources.

**No SVG is an error image** — all 24 exceed 36 KB and none contains `syntax error` / `Error line`:

```
01-c4-context             96,059      13-seq-upload-document       169,073
02-c4-container          170,965      14-seq-compaction            185,326
03-c4-component-orch     623,362      15-seq-auth                  164,247
04-c4-component-sync     585,947      16-activity-lifecycle        104,369
05-c4-component-front    440,180      17-state-conversation         61,050
06-deployment            132,692      18-er-data-model              98,322
07-seq-chat-stream       166,167      19-dfd-trust-boundaries      100,607
08-seq-router-dispatch   131,423      20-class-engines             130,802
09-seq-agent-loop        164,103      21-network-ports              36,359
10-seq-text-to-sql        93,277      22-threat-model               39,139
11-seq-rag                76,683      23-cicd-test-topology         60,692
12-seq-sf-sync           107,384      24-remediation-gantt          79,343
```

### ❌→✅ Failure found and fixed: invalid sprite names

`04-c4-component-syncworker.puml` initially failed `-checkonly`. Root cause: the bundled tupadr3 registers
font-awesome-5 sprites under their **bare** name (`hdd`, `database`, `cogs`), **not** `fa5_hdd`. Confirmed with
`listsprites`:

```
include hdd -> sprite name(s): hdd
```

15 `$sprite="fa5_*"` references across 2 files were rewritten to bare names. This mattered beyond the one hard
failure: a wrong sprite name normally renders as a **silently missing icon** rather than an error, so the rest
would have shipped without their icons. Re-checked: **24/24 clean.** Documented in
[`02-diagrams/_STYLE.md`](02-diagrams/_STYLE.md).

Also verified: every `@startuml <name>` matches its filename, so `render/` filenames match `src/` (PlantUML
names output from the `@startuml` token, not the file).

---

## ✅ 4. Zero local includes and zero `!includeurl`

```bash
grep -rn '!include' docs/02-diagrams/src/ | grep -v '!include <'   # → NONE
grep -rn 'includeurl' docs/02-diagrams/src/                        # → NONE
```

**PASS.** Every source is standalone and draw.io-importable; the shared style block is duplicated verbatim into
all 24 files rather than `!include`d.

A separate grep for `http://` returns **10 hits** — all internal service endpoints used as *arrow-label text*
in diagrams 06 and 21 (e.g. `http://vllm:30000`). Nothing resolves them, so they are harmless. The
`02-diagrams/README.md` wording was corrected to state this precisely rather than claim a blanket
"no URLs anywhere".

---

## ✅ 5. Entity names and colours are consistent across all diagrams

All six palette colours appear in **24/24** files:

```
#2563EB 24/24    #7C3AED 24/24    #16A34A 24/24
#D97706 24/24    #64748B 24/24    #DC2626 24/24
```

Shared scaffolding present in **24/24**: `!theme plain` · `Layer palette` block · `title` · `legend right`.

Canonical entity names are used wherever the entity appears (counts differ only because not every diagram
covers every subsystem — e.g. `vllm-router` is irrelevant to the ER diagram):

```
orchestrator 24/24 · DuckDB 16/24 · sync-worker 14/24 · LanceDB 11/24 · vllm-embed 10/24 · vllm-router 7/24
```

**No naming mismatches found** — no `orchestrator-api` / `the orchestrator` / `FastAPI service` variants.

**Node counts** are within the ~25 guideline except `03-c4-component-orchestrator` (28), kept whole
deliberately because a complete engine↔core map is more useful than a split one; noted in `_STYLE.md`.

---

## ✅ 6. Every finding's `file:line` exists — 12 spot-checked at random

| ID | Reference | Line content at that reference |
|---|---|---|
| SEC-01 | `orchestrator/app/auth.py:95` | `def require_user(request: Request) -> sqlite3.Row:` |
| SEC-07 | `orchestrator/app/core/sql_guard.py:87` | `if c == "'":` |
| DATA-04 | `sync-worker/syncworker/storage.py:38` | `def normalize_records(records: list[dict]) -> list[dict]:` |
| PERF-01 | `orchestrator/app/engines/sql.py:201` | `columns, rows = _execute(sql, cap)` |
| SEC-02 | `orchestrator/app/main.py:338` | `try:` |
| PERF-02 | `orchestrator/app/core/net.py:153` | `body = resp.content[: max_bytes + 1]` |
| DATA-03 | `orchestrator/app/db.py:334` | `def delete_conversation(user_id: int, conversation_id: str…` |
| PERF-04 | `orchestrator/app/llm.py:72` | `def _client(base_url: str, api_key: Optional[str] = None):` |
| PERF-05 | `orchestrator/app/core/extract.py:55` | `def _extract_pdf_text(body: bytes) -> str:` |
| QUAL-02 | `orchestrator/app/core/sql_guard.py:160` | `def is_safe_select(sql: str) -> bool:` |
| REL-02 | `docker-compose.yml:291` | `sync-worker:` |
| SEC-06 | `.env.example:7` | `# AWS credentials — used ONLY to read the Salesforce sec…` |

**12/12 exact** — each line contains precisely the construct the finding describes.

---

## ✅ 7. No secret values anywhere in `docs/`

Every secret-bearing key in `.env` was read and its **value** grepped across the whole of `docs/`:

```
HF_TOKEN         : clean (value len 37 not present in docs/)
SF_CLIENT_SECRET : clean (value len 64 not present in docs/)
SF_CLIENT_ID     : clean (value len 85 not present in docs/)
SEARXNG_SECRET   : clean (value len 64 not present in docs/)
SF_USERNAME      : clean (value len 43 not present in docs/)
SF_LOGIN_URL     : clean (value len 34 not present in docs/)
```

**PASS.** Key *names* appear freely (13 files mention `HF_TOKEN`), which is required by the brief; no value does.

A broader scan initially flagged two keys — investigated and both are **false positives**, not secrets:
`CHART_TRIGGER_MODE=explicit` (a mode name; the word "explicit" occurs in prose) and
`SEARXNG_URL=http://searxng:8080` (a service address also published in `.env.example` and `docker-compose.yml`).

---

## ✅ 8. `git status` shows no modified files outside `docs/`

```
 M .env.example                              ?? frontend/components/ChartErrorBoundary.tsx
 M CHANGELOG.md                              ?? frontend/components/EChart.tsx
 M README.md                                 ?? frontend/lib/chartFormat.ts
 M frontend/components/ChartView.tsx         ?? frontend/lib/chartOption.ts
 M frontend/components/ProofDrawer.tsx       ?? frontend/lib/chartTheme.ts
 M frontend/lib/types.ts                     ?? frontend/tests/chartOption.test.ts
 M frontend/package-lock.json                ?? orchestrator/app/core/chart_data.py
 M frontend/package.json                     ?? orchestrator/app/core/chart_decision.py
 M orchestrator/app/config.py                ?? orchestrator/app/core/chart_pipeline.py
 M orchestrator/app/core/chart_spec.py       ?? orchestrator/app/core/chart_profile.py
 M orchestrator/app/core/charts_png.py       ?? orchestrator/tests/test_chart_data.py
 M orchestrator/app/engines/agent.py         ?? orchestrator/tests/test_chart_decision.py
 M orchestrator/app/engines/report.py        ?? orchestrator/tests/test_chart_pipeline.py
 M orchestrator/app/engines/sql.py           ?? orchestrator/tests/test_chart_routes.py
 M orchestrator/tests/test_agent.py          ?? orchestrator/tests/test_charts_png.py
 M orchestrator/tests/test_chart_spec.py     ?? orchestrator/tests/test_report_charts.py
 M orchestrator/tests/test_config.py
 ?? docs/
```

```
entries outside docs/ : 33   (17 modified + 16 untracked)
docs/ additions       :  1
```

**PASS.** These 33 entries are **byte-identical to the pre-audit baseline** captured before any work began — an
in-progress charting feature that was already uncommitted (see [`ASSUMPTIONS.md#a7`](ASSUMPTIONS.md)). That
uncommitted code **was** audited; it is live code on `main`. The audit's only contribution to the working tree
is `docs/`.

No source file was created, modified, formatted or deleted. No dependency was installed. No `git` state was
mutated. `plantuml.jar` lives in the session scratchpad, outside the repository.

---

## ✅ 9. Severity distribution

| Severity | Count |
|---|---:|
| **P0 Critical** | 1 |
| **P1 High** | 7 |
| **P2 Medium** | 22 |
| **P3 Low** | 8 |
| **Total** | **38** |

The brief notes that zero P0s **and** zero P1s would require justification. **Not applicable — 1 P0 and 7 P1s
were found**, which matches the expectation for a system with GPU services, external auth, web ingestion and
LLM-generated SQL.

Consolidated from **162 raw candidate findings** across 12 independent subsystem readers, after de-duplication
(five readers independently reported the same auth/port exposure) and adversarial verification.

### Claims that were escalated or downgraded during verification

| Raw claim | Verdict | Why |
|---|---|---|
| `sql_guard` is safe | **Escalated to P1 (`SEC-07`)** | A real bypass was found *and executed*: `SELECT E'\'' , 1; DROP TABLE t` passes the guard, defeating even the `;` multi-statement check |
| `sql_guard` bypass is P0 | **Held at P1** | All four payloads were then run against DuckDB configured as `sql.py:124-132` does — every one refused (`PermissionException`, `InvalidInputException … read-only`). Defence-in-depth holds, so it is not currently exploitable |
| Everything-is-VARCHAR is P0 | **Held at P1 (`DATA-04`)** | Measured in DuckDB 1.5.4: `ORDER BY` and `MAX` are silently wrong, `SUM` fails loudly. Severe correctness defect, but not a security breach |
| Live SOQL has no guard (P0) | **Downgraded to P2 (`SEC-08`)** | The cited file was wrong — `guard_soql` exists at `core/salesforce.py:55`, enforcing single-statement, `SELECT`-only, keyword blocklist and a forced `LIMIT`. The real gap is only the missing object allowlist, bounded by a read-only integration user |
| PDF rasterisation is P0 host-memory exhaustion | **Downgraded to P2 (`PERF-05`)** | Real waste (`_images` discarded at `extract.py:60`) but bounded by `max_pages=10` |
| Exports land in an auth-free `/reports` (P0) | **Merged into `SEC-01`** | A consequence of the missing auth, not an independent finding |

### Findings added in a second verification round

The documentation and diagram passes surfaced five further defects that the first reader pass had missed. Each
was independently verified before being accepted:

| ID | Verification performed | Result |
|---|---|---|
| `SEC-11` | `grep -c '^USER'` across all three Dockerfiles | orchestrator **0**, sync-worker **1** (`USER worker`), frontend **1** (`USER nextjs`) — the orchestrator runs as root |
| `DX-03` | `grep multipart` on both requirement files | present in `requirements.txt:5`, **absent** from `requirements-dev.txt` — yet needed at import time |
| `DX-04` | `grep -c env_file docker-compose.yml` | **0** — only explicitly interpolated `${VAR}` values reach a container |
| `TEST-03` | Read the four cited assertions | confirmed: `assert True`, `assert … in (True, False)`, `assert … is None or True`, and a checker monkeypatched to itself |
| `DOC-02` | `grep SEARCH_MAX_RESULTS README.md .env.example` | `README.md:142` says `10`; `.env.example:55` says `100` |

`DX-03` also **corrected a defect in this report's own recommendation**: the CI workflow proposed under
`TEST-01` originally installed `requirements-dev.txt` alone, which would have failed at collection on a clean
runner. The snippet now installs `python-multipart` explicitly.

### Findings investigated and **dropped** because the code proved correct

Recorded so their absence reads as verified rather than missed — full detail in
[`ASSUMPTIONS.md#a12`](ASSUMPTIONS.md):

- **Model-output XSS** — `Markdown.tsx` omits `rehype-raw`; react-markdown **10.1.0** applies
  `defaultUrlTransform` (`safeProtocol = /^(https?|ircs?|mailto|xmpp)$/i`), neutralising
  `[x](javascript:…)`; `MermaidBlock.tsx:58` sets `securityLevel: 'strict'`.
- **DuckDB replacement-scan bypass** — `SELECT * FROM '/etc/passwd'` does pass the regex guard, but DuckDB
  refuses it (`enable_external_access=False`). Reported only as a defence-in-depth note.
- **Sync watermark data loss on crash** — it does not lose data. `syncworker/main.py:167` upserts *before*
  `:188` commits the watermark, and the upsert is `DELETE`+`INSERT` in one transaction. Recorded as a
  **strength**.
- **Unbounded live-generation registry** — `_live_generations` is popped in `_finalize_generation`
  (`main.py:151-152`). No leak.
- **Module-level dead code** — none. Every module is imported or is a documented entrypoint/CLI. The only dead
  symbol is `is_safe_select` (`QUAL-02`).

---

## ✅ 10. Tests were executed, not assumed

Run with each project's own pre-existing virtualenv; **nothing was installed**.

| Suite | Command | Result |
|---|---|---|
| orchestrator | `./.venv/bin/python -m pytest -q` | **800 passed** in 41.57 s |
| sync-worker | `./.venv/bin/python -m pytest -q` | **104 passed** in 1.06 s |
| frontend | `npx vitest run` | **237 passed**, 16 files, 562 ms |
| **Total** | | **1,141 passed · 0 failed** |

The frontend suite's 137 ms of actual test execution confirms it is entirely unit-level — evidence for
`TEST-02` (no end-to-end coverage), not a criticism of the tests that exist.

`pytest --timeout` was rejected (`pytest-timeout` not installed) and the flag was **dropped rather than
installing a plugin into the project**.

---

## ✅ 11. Deliverable tree complete

```
docs/README.md                     ✓      docs/02-diagrams/README.md          ✓
docs/ASSUMPTIONS.md                ✓      docs/02-diagrams/_STYLE.md          ✓
docs/00-INVENTORY.md               ✓      docs/02-diagrams/render.sh          ✓
docs/04-VERIFICATION.md            ✓      docs/02-diagrams/src/*.puml     24/24
docs/01-codebase/*                13/13   docs/02-diagrams/render/*.svg   24/24
docs/03-report/IMPROVEMENT-REPORT  ✓      docs/02-diagrams/render/*.png   24/24
docs/03-report/FINDINGS.csv        ✓
docs/03-report/JIRA-BACKLOG.md     ✓
docs/03-report/QUICK-WINS.md       ✓
```

`docs/_evidence/` additionally holds the 12 raw per-subsystem evidence documents (≈9,000 lines) the
documentation was written from, kept for traceability.

---

## Final console summary

```
Files audited ....... 251  (43,189 LOC — Python 24,240 · TS 9,152 · TSX 5,667)
Docs written ........  20  (inventory + 13 module docs + 4 report files + README + assumptions + verification)
Diagrams produced ...  24  (24 .puml · 24 SVG · 24 PNG — all pass -checkonly)
Tests executed ...... 1,141 passing, 0 failing
Findings ............  38  (P0: 1 · P1: 7 · P2: 22 · P3: 8)
```

### Top 3 things to fix this week

1. **`SEC-01` — bind the published ports to `127.0.0.1`, or delete the four vLLM `ports:` blocks entirely.**
   Six lines of YAML. The platform currently has no authentication and is reachable from any host on the
   network. This is the highest security-return-per-minute change available anywhere in the codebase.
2. **`PERF-01` — wrap two calls in `asyncio.to_thread`** (`engines/sql.py:201,206`). Two lines; stops one slow
   query from freezing every other user's token stream.
3. **`TEST-01` — commit the CI workflow.** The 1,141 tests already pass in under a minute; make them run
   themselves.
