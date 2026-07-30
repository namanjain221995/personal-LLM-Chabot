# Assumptions & Judgement Calls

Every ambiguous decision taken during this audit, with the reasoning. Nothing here is a
finding; findings live in [`03-report/IMPROVEMENT-REPORT.md`](03-report/IMPROVEMENT-REPORT.md).

---

## A1 — Audit scope: 251 files

**Call.** "Read everything" was scoped to **251 files**: every `.py`, `.ts`, `.tsx`, `.mjs`,
`.mts`, `.yml`, `.yaml`, `.css`, `.json`, `.txt`, `.md`, `Dockerfile`, `.env.example`,
`.gitignore`, `.dockerignore` and `*.bak*` in the repository.

**Excluded, and why.**

| Excluded | Reason |
|---|---|
| `node_modules/` | Third-party; the brief excludes it |
| `.git/`, `screenshots/`, `vllm_models/`, `.next/` | Excluded by the brief |
| `orchestrator/.venv/`, `sync-worker/.venv/` | **Not named in the brief but excluded anyway.** Both are committed-to-disk virtualenvs holding ~12,300 vendored library files (pandas, numpy, matplotlib, lancedb…). Auditing them would mean auditing PyPI, not this codebase. Their *declared* dependencies are audited instead, from `requirements*.txt`. This single exclusion is the difference between 251 and ~12,585 files |
| `__pycache__/`, `.pytest_cache/` | Build artefacts |
| `frontend/package-lock.json` (18k lines), `tsconfig.tsbuildinfo` | Generated lockfile/artefact; the *dependency set* is audited from `package.json` and the lockfile's existence is treated as a fact about reproducibility |

**Verification.** `docs/04-VERIFICATION.md` prints the `find` count against the inventory
row count; they must match.

---

## A2 — Secret handling: names only, never values

**Call.** `.env`, `.env.bak-205921` and `secrets/` were opened **only** to enumerate
variable *names*. No value appears anywhere in `docs/`.

**What was found and how it is reported.** `.env` and `.env.bak-205921` exist in the working
tree with real credentials in them. Both are correctly matched by `.gitignore` (`.env`,
`.env.*`, `*.bak-*`) and `git ls-files` confirms neither is tracked. `secrets/` is present,
mode `drwx------`, and **empty**. Because these files are untracked and unreadable to anyone
without host access, the *existence* of a plaintext-credential backup is reported as a
finding, but no value is transcribed. `.env.example` is tracked and, correctly, contains only
placeholders.

---

## A3 — PlantUML toolchain: JAR + Smetana, not a system package

**Call.** Neither `plantuml` nor `graphviz` is installed on this host, and `apt-get` requires
a password this session cannot supply. Rather than skip rendering:

1. `plantuml-1.2024.7.jar` (22 MB) was downloaded and is driven by the host JRE
   (OpenJDK, `/usr/bin/java`). It is kept **outside the repository**, in the session
   scratchpad, so no binary is added to the project.
2. `graphviz` (`dot`) is absent, so layout uses PlantUML's built-in **Smetana** engine via the
   command-line flag `-Playout=smetana`.

**Why the flag is on the command line and not in the files.** Writing `!pragma layout smetana`
into each `.puml` would make the sources depend on a local rendering choice. draw.io renders
with its own graphviz and does not need it. The flag therefore lives only in
[`02-diagrams/render.sh`](02-diagrams/render.sh); the 24 sources stay clean and portable.

**Consequence.** Rendering succeeded — the diagrams are real SVG and PNG, not `RENDER-SKIPPED`.
Smetana's node placement differs slightly from graphviz's, so a diagram opened in draw.io may
lay out a little differently than the checked-in SVG. Content is identical.

---

## A4 — `plantuml -checkonly` exit code is unreliable

**Call.** `-checkonly` returns **exit code 0 even when a diagram fails to parse** (verified: a
file with a bad `!include` printed `Error line 22` and still exited 0). Correctness is
therefore judged by **grepping the command's output for `error`**, never by `$?`.
`render.sh` implements it that way and refuses to render if any file fails.

---

## A5 — AWS icons are deliberately absent from the diagrams

**Call.** The brief allows AWS stdlib icons "only where AWS is genuinely used". **AWS is not
used.** `sync-worker/syncworker/secrets.py:3` records that Secrets Manager was removed at the
owner's request, `docker-compose.yml:294` repeats it, and `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION` and `SF_SECRET_NAME` have **zero** references anywhere
outside `.env.example`. No AWS icon appears in any diagram.

Diagram 15 (auth/secrets) therefore shows the **real** resolution order — direct env vars, a
mounted PEM file, or base64 — with the removed AWS path drawn as an explicitly dead branch,
because `.env.example:7-14` still advertises it. That drift is itself a finding.

---

## A6 — Sprite names were probed, not assumed

**Call.** Guessing stdlib sprite names produces diagrams that fail to render. Every icon used
was verified against the installed stdlib by compiling a one-line probe diagram per candidate.
Confirmed present: `devicons2/{python,nextjs,typescript,docker,nodejs,postgresql,react_original}`,
`logos/{salesforce,python,nextjs,react,typescript,docker-icon,nvidia}`,
`font-awesome-5/{database,microchip,server,lock,shield_alt,search,file_alt,chart_bar,user,robot,cloud,exclamation_triangle,network_wired,hdd,cogs}`,
`material/{memory,database}`, all five `C4/*` modules.

Confirmed **absent** in this stdlib version, and therefore never used:
`devicons2/react` (the real name is `react_original`), `devicons2/nextjs_original`,
`logos/duckdb`, `awslib14/SecurityIdentityCompliance/SecretsManager`,
`awslib14/Storage/SimpleStorageService`. DuckDB, which has no icon anywhere in the stdlib, is
drawn with `font-awesome-5/database` in the shared `$C_DATA` colour in **all** diagrams.

---

## A7 — The working tree was already dirty before the audit

**Call.** `git status` was **not** clean at the start: 17 modified files and 16 untracked files,
all part of an in-progress charting feature (`chart_decision.py`, `chart_pipeline.py`,
`EChart.tsx`, `chartOption.ts` …). This is a **pre-existing baseline**, not audit damage.

**Consequence.** The rule "no modified files outside `docs/`" is verified as *"the set of
changed paths outside `docs/` is identical to the pre-audit baseline"*, and the baseline is
printed in `04-VERIFICATION.md`. The uncommitted charting code **is** in scope and **was**
audited — it is live code on `main`.

---

## A8 — Tests were executed, not just read

**Call.** The brief says read-only on source. Running the existing suites mutates nothing, and
"the tests pass" is a claim that must be verified rather than assumed. All three suites were
run with their own pre-existing virtualenvs (`./.venv/bin/python`); **nothing was installed**.

Measured: **800 passed** (orchestrator, 41.6 s) · **104 passed** (sync-worker, 1.1 s) ·
**237 passed** (frontend Vitest, 16 files). Total **1,141 passing, 0 failing**.

`pytest --timeout` was rejected (`pytest-timeout` is not installed) and the flag was dropped
rather than installing a plugin into the project.

---

## A9 — "14 engines" is 13 engine modules plus a dispatcher

**Call.** The brief names 14 engines. `orchestrator/app/engines/` contains **13** engine
modules — `router`, `orchestrate`, `agent`, `chat`, `sql`, `rag`, `search`, `repo`, `url`,
`dataset`, `document`, `vision`, `report`, `live_sf` — of which `router` and `orchestrate`
are **not** engines: they classify and plan, expose no `emit` parameter, and stream nothing.
The count is documented as observed rather than forced to 14, and `20-class-engines.puml`
draws the real split: 11 streaming engines + 2 control modules + `live_sf` as a helper
invoked from inside `sql`.

---

## A10 — There is no engine base class to document

**Call.** `orchestrator/app/engines/__init__.py` defines no ABC and no `Protocol`. The engine
"contract" is duck typing: the alias `Emit = Callable[[str, dict], Awaitable[None]]` is
re-declared independently in at least three files. `20-class-engines.puml` shows the
*implied* interface as a dashed stereotype marked `«implicit — not enforced»`, so the diagram
is not read as claiming a base class exists.

---

## A11 — Severity is calibrated to the stated deployment, and the stated deployment is contradicted

**Call.** `orchestrator/app/auth.py:17-20` says there is no authentication and that this "is
fine for a machine only you can reach, and NOT fine if the port is published to a network you
do not control — see the compose port bindings." The compose port bindings publish
`8080:8080` and `3000:3000`, which Docker binds to **`0.0.0.0`**, i.e. every host interface.

Severity is therefore rated against **what the code actually does**, not the intent: the file's
own stated precondition for "fine" is not met. Where a risk depends on reachability it says so
explicitly, so a reader who genuinely runs this on an isolated host can re-rate it themselves.

---

## A12 — Findings are evidence-bounded, and some plausible ones were dropped

**Call.** Several natural-sounding findings were **investigated and discarded** because the
code was correct. They are recorded here so the absence is understood as verified rather than
missed:

- **Markdown/model-output XSS** — `Markdown.tsx` does not use `rehype-raw`, and
  react-markdown **10.1.0** applies `defaultUrlTransform`
  (`safeProtocol = /^(https?|ircs?|mailto|xmpp)$/i`), so `[x](javascript:…)` is neutralised.
  `MermaidBlock.tsx:58` sets `securityLevel: 'strict'`. Not a finding.
- **DuckDB replacement-scan bypass** — `SELECT * FROM '/etc/passwd'` does pass `sql_guard`
  (string-literal contents are stripped before keyword scanning), but
  `engines/sql.py:124-132` opens DuckDB with `read_only=True`,
  `enable_external_access=False` and both autoload flags off, so DuckDB refuses it. Reported
  only as a defence-in-depth note, not an exploitable bypass.
- **Sync watermark loses data on crash** — it does not. `syncworker/main.py:167` upserts
  before `:188` commits the watermark, and the upsert is `DELETE`-then-`INSERT` in one
  transaction, so a crash re-fetches and re-applies idempotently. Recorded as a **strength**.
- **Unbounded live-generation registry** — `_live_generations` is popped in
  `_finalize_generation` (`main.py:151-152`). No leak.
- **`_live_generations` cross-user leak** — a conversation-ownership check exists
  (`main.py:339-344`). Its *exception handling* is a finding; the check's presence is not.

---

## A13 — Documentation-only writes

No file outside `docs/` was created, modified, formatted or deleted. No dependency was
installed into `orchestrator/`, `sync-worker/` or `frontend/`. No `git` state was mutated —
no `add`, `commit`, `checkout`, `stash` or `clean`. `plantuml.jar` lives in the session
scratchpad, not the repository.
