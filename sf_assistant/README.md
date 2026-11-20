# sf_assistant

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

The Salesforce assistant as one Python package, with a small web page where signed-in users
can ask it questions.

Answers come from the **object knowledge files** (what the objects, fields, values, relationships and automation
mean) and from a **read-only DuckDB copy of the warehouse** (preprod sandbox records). The local main model
(`Qwen/Qwen3.6-35B-A3B-NVFP4` at `http://localhost:8000/v1`) makes every decision through typed tools. Code checks
every object, field and value it picks, and builds every SQL statement with bound parameters. The model never
writes SQL.

Where the restructure stands (2026-10-20):

- **Stage 1, done:** the pipeline of `notebooks/object_knowledge_eval` moved into `src/sf_assistant/`, module by
  module, with only the imports and the paths changed. Its parity with the notebook folder is proven. The notebook
  folder stays as it was.
- **Stage-2 cleanups, done:** each limit lives in one module; the facades are slimmed; one `Settings` object is
  wired through the cli and the pipeline; the lines code writes are prompt files. Behaviour did not change, and the
  parity harness was re-run.
- **Stage 2, done:** the page imports this package instead of the notebook folder's `ask.py`;
  the MCP server is the container `sf-assistant-mcp` (`http://<SFA_MCP_BIND>:8700/mcp`
  with a bearer token; also `python -m sf_assistant.server --stdio`); the evaluation runner
  (`python -m sf_assistant.evaluation run --set ... --mode ... --level ...`) is built and tested with the mock model.
  The fixes of the stage-2 review (2026-10-20) are applied; parity with the notebook folder still holds.
- **Stage 3, wired (2026-10-20, review fixes 2026-10-21), on by default since 2026-10-27:** the fuzzy term resolver
  ([docs/resolver.md](docs/resolver.md), plan and status [docs/resolver_integration.md](docs/resolver_integration.md))
  reads misspelt object, field, value and person names ("accont", "Completd", "Dellta Persn") in the question hints of
  both agents, the BM25 searches, the record tools and the people tools; a clear correction is used and stated (the
  tool note and one "Read as: ..." line in the answer; a person: the lead sentence), anything less clear becomes a
  did-you-mean; a number is never read as another number, and romanized Hindi question words are correct words. It
  runs unless `SFA_RESOLVER=off` (the owner's order of 2026-10-26, after the evening evaluation; it needs an English
  word list, the image gets `wamerican wbritish`). With it off nothing changes (parity holds; the parity harness and
  the evaluation's reference replay always run with it off; [how](docs/resolver.md#how-to-switch-it-off)). The
  knowledge tools' object and field arguments and the E4h close spellings use it too (2026-10-21).
- **Group B people fixes (2026-10-21), built, reviewed and checked model-free, not yet evaluated:** general fixes
  for the round-2 people causes ([docs/people.md](docs/people.md)): about_people states linked records, distinct
  counts, parent paths, the other people in a role, same-name owners and partial copies; lists show the person's
  other rows; draft checks for relation words and direction, parent paths, partial copies and a corrected first
  name several people share; a status line when a count can mean finished records; a first name alone keeps the
  record agent when the data allows one reading. This changes answers with the switch off on purpose; the parity
  harness explains each difference (`.private/parity_2026-10-21_groupb_review.md`). The page and the MCP server
  still run the code they loaded before.
- **Evaluation fixes (2026-10-22), built, reviewed and checked model-free, not yet evaluated:** the six fixes of the
  evening evaluation ([docs/people.md](docs/people.md#evaluation-fixes-2026-10-22)): a profile keeps the first 12
  filled values per record (the employees' joining date is back); an answer with nothing but its Source line is never
  shown; a merged count-0 clause that a clause of the record answer holds with the same claim is no reason to reject
  the merge (an any-role clause is corrected in both texts instead); a record joins a person only through that
  person's identity links, and a record that several listed people's records reach is nobody's own (listed once, as
  shared); a relation word of the question checks the lookup the knowledge words it for; the Application Tracking
  knowledge says that one `Application__c` record is one daily update (applications sent = the sum of
  `Total_Applications__c`). The review of the same night (15 findings) and a second review (2026-10-23, 11 findings:
  a shared record also links nobody and counts for no ranking; the people-answer fallback reads a misspelt name's
  stored spelling) are applied; owner decisions are open
  (fix 6 against three application answer keys; one relation-lookup rule). They also change answers with the switch
  off on purpose. The page and the MCP server run them only after their next restart.
- **Value spots (2026-10-28), built and checked model-free, not yet evaluated:** code recognises every stored record
  value a question names, in every queryable object and field (Names, codes, short text;
  [docs/value_spots.md](docs/value_spots.md)). A strong one sends a question the route call judged design-only to the
  records too; the record agent's hints say where each named record is stored and which records link to it, with
  counts, and the trace and the log say why the route was overruled. Picklist values, record type names and plain
  words never overrule a route, and people keep their own path. The page and the MCP server run it only after their
  next restart.
- **Charts (2026-11-03), on since the owner's go-live of 2026-11-04:** with `SFA_CHARTS=on`, the default, a question that
  asks for a chart, graph or plot gets the picture and the table of its numbers, or one plain line saying why not
  ([docs/runbook.md](docs/runbook.md#charts-sfa_charts)). Code draws it from the record results (`agent/charts.py`, an
  SVG, no new dependency); the model fetches the data as it does today and types no chart number. The page, the cli and
  the sidecars get it, and MCP clients ask with `chart: true` on three record tools. A real model test on 2026-11-04 drew
  30 charts, and all 341 chart cells appear in the answer text. In a terminal or an evaluation run, `SFA_CHARTS=off`
  turns them off; the page and the MCP server take `CHARTS_DEFAULT`.

## Docs

| Page | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Diagrams, where things run, one question end to end, the module map, configuration |
| [docs/data_and_privacy.md](docs/data_and_privacy.md) | A short note: what this repository holds, what stays blocked, MCP clients, logs |
| [docs/runbook.md](docs/runbook.md) | Start, stop and status of the page and the MCP server, the firewall, rotating codes and the token, terminal questions, the charts switch, evaluations, rebuilding the knowledge, tests |
| [docs/tools.md](docs/tools.md) | Every tool a model can call (generated from `tools/registry.py`) |
| [docs/prompts.md](docs/prompts.md) | Every prompt id with its version and hash (generated from `prompts/*.md`) |
| [docs/value_spots.md](docs/value_spots.md) | The value spots: the entity index of the stored values, how a question's values are spotted and how strong they are, the route overrule, the hint section, the trace and the log |
| [docs/people.md](docs/people.md) | The Group B people fixes and the evaluation fixes of 2026-10-22: what each item changed, the result keys, the profile and answer-choice rules, where the build differs from the plan, what is open |

## Layout

```
sf_assistant/
  README.md, pyproject.toml   package sf-assistant (src layout; pins = web/requirements.lock, test extra)
  .gitignore                  .private/, .venv/, /knowledge/, /kb_build/, /docs/spec/, /tests/parity/, /web/.env, *.jsonl, *.duckdb, transcripts,
                              reports/ (knowledge evaluation runs)
  .private/                   git-ignored, mode 700: real names and record data (codes.json, web_log.jsonl,
                              mcp_token.json, mcp_audit_key, mcp_log.jsonl, ask_token.json, ask_log.jsonl, questions/,
                              okeval_run/ (snapshot,
                              lookup and people sets), runs/ (runs/eval/: evaluation runs), parity reports and
                              parity/harness/, rollback_20261020/)
  ask.sh                      the cli: .venv/bin/python -m sf_assistant ask
  src/sf_assistant/           config, prompts, knowledge/, engine/, tools/, agent/ (with charts: the charts behind
                              SFA_CHARTS), evaluation/ (runner, graders, sets.yaml, knowledge_eval, timeline_report),
                              server/ (MCP server: app, data, auth, audit), pipeline, cli
  prompts/                    every prompt text as <id>.md (96 files; docs/prompts.md)
  knowledge/objects/          the object knowledge files (git-ignored, built by kb_build/)
  knowledge/components/       the flow component files flows/<ApiName>.json and index.json (git-ignored, built by
                              kb_build/flows/; read by the component tools, docs/tools.md)
  kb_build/                   not in this repository: builds knowledge/objects/ and knowledge/components/
  scripts/                    gen_tools_doc.py (docs/tools.md), gen_prompts_doc.py (docs/prompts.md); both have --check
  docs/                       architecture, data_and_privacy, runbook, tools (generated), prompts (generated), resolver,
                              value_spots
  web/
    server.py                 the page (FastAPI): sign-in, status, ask, queue, rate limit, log, security headers
    pipeline.py               builds the pipeline once, answers one question, reopens on a new read copy
    ask_mcp.py, compose.ask.yaml  the ask service: one MCP tool for /chat, its own container, a Unix socket and no
                              TCP port (docs/runbook.md)
    codes.py                  access codes: generate, store as scrypt hashes, verify
    static/                   index.html, app.css, app.js, render.js (safe markdown), favicon.svg; no CDN
    requirements.txt, .lock   top-level pins; the lock pins every package (image and test venv)
    Dockerfile, compose.yaml  python:3.11-slim with exactly the lock; compose project sf-assistant, services
                              sf-assistant-web (the page) and sf-assistant-mcp (the MCP server, same image)
    run.sh                    page: up | logs | status | new-codes | restart-page;
                              MCP: up-mcp | mcp-logs | mcp-status | new-mcp-token [--add] [--label NAME] | restart-mcp;
                              ask service: up-ask | restart-ask | new-ask-token
  tests/                      fake records only: ported pipeline tests, test_web.py, test_charts.py, test_charts_flow.py,
                              render_check.js, golden/ hashes
```

The package only runs from this folder: `prompts/`, `knowledge/objects/` and `.private/` are outside
`src/sf_assistant`. Install it editable (`-e .`), or set `SFA_HOME` to this folder.

## Quick start

The page (`http://<SFA_BIND>:8600/`; the address defaults to `127.0.0.1`, this machine only: set it in
`web/.env`, see `web/.env.example`):

```bash
cd sf_assistant/web
./run.sh up          # build and start (docker compose -p sf-assistant up -d --build)
./run.sh status      # container state and /api/status
./run.sh new-codes   # owner's own terminal only: prints the access code and the admin code once
```

The MCP server (`http://<SFA_MCP_BIND>:8700/mcp`; clients and the firewall first: [docs/runbook.md](docs/runbook.md#the-mcp-server-sf-assistant-mcp)):

```bash
cd sf_assistant/web
./run.sh up-mcp          # build the image and start only the MCP container (the page is not recreated)
./run.sh mcp-status      # container state and /healthz
./run.sh new-mcp-token   # owner's own terminal only: prints the bearer token once
```

An evaluation (only when the owner asks; it uses the model the page uses):

```bash
.venv/bin/python -m sf_assistant.evaluation run --set all --level quick   # 35 questions, merged
```

One knowledge file on its own: the notebook folder's `run_local.py` evaluation, same options and output files
(questions built from the file, answers from the knowledge tools only, grades, coverage, knowledge gaps; results in
`reports/knowledge_eval/<object>_<time>/`, git-ignored; details in
[docs/runbook.md](docs/runbook.md#one-knowledge-file-on-its-own)):

```bash
.venv/bin/python -m sf_assistant.evaluation knowledge --dry          # the mock model: no network, scores mean nothing
.venv/bin/python -m sf_assistant.evaluation knowledge --knowledge knowledge/objects/Session__c.json   # the model (owner's run)
```

The terminal:

```bash
./ask.sh "How do I stop the background check reminders?"
.venv/bin/python -m sf_assistant ask --help
```

The spelling help (term resolver), on by default:

```bash
./ask.sh "how many interviews are Completd"                    # start-up line "spelling help on (...)"
SFA_RESOLVER=off ./ask.sh "how many interviews are Completd"   # without it
cd web && SFA_RESOLVER=off ./run.sh up   # the page and the MCP server without it: rebuilds the image, signs everyone out
```

The tests (no model, no warehouse):

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests                    # switch on (the default)
SFA_RESOLVER=off PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests   # switch off
```

The lock (`web/requirements.lock`, the image's and the test venv's exact packages) is regenerated as in
[docs/runbook.md](docs/runbook.md#the-venv-and-the-lock): tested pins kept as constraints, no release younger than
two weeks, then `pyproject.toml` updated to the same lines.

Read the rules in [docs/runbook.md](docs/runbook.md#rules) before you change anything that runs. In short: always
name the `sf-assistant` compose project; never touch the `sf-local-ai` stack or `/data/warehouse.duckdb`; never
delete the logs, `codes.json` or the MCP token file; run `new-codes` and `new-mcp-token` only in the owner's own
terminal; and run no model evaluation during development.

See [docs/data_and_privacy.md](docs/data_and_privacy.md).
