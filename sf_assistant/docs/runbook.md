# Runbook

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

How to start, stop and check the page and the MCP server, rotate the codes and the token, ask from the terminal, run
an evaluation, rebuild the knowledge and run the tests. Commands run from the `sf_assistant` folder unless they say
otherwise. State on 2026-10-20: stage 2 is done (the page runs on this package, the MCP server and the evaluation
runner are built and tested), and the stage-2 review fixes are applied.

| What | Where | Start |
|---|---|---|
| LAN page | container `sf-assistant-web`, `http://<SFA_BIND>:8600/` | `web/run.sh up` |
| MCP server, HTTP | container `sf-assistant-mcp`, `http://<SFA_MCP_BIND>:8700/mcp` | `web/run.sh up-mcp` |
| Ask service (one MCP tool for `/chat`; built 2026-11-04, not started, nothing calls it yet) | container `sf-assistant-ask`, no TCP port: the Unix socket `/run/sf-assistant-ask/ask.sock` in the volume `sf-local-ai_ask-sock` | `web/run.sh up-ask` |
| MCP server, stdio, live read copy | a process the MCP client starts in the container | `docker exec -i sf-assistant-mcp python -m sf_assistant.server --stdio` |
| MCP server, stdio, local snapshot | a process the MCP client starts on this machine | `.venv/bin/python -m sf_assistant.server --stdio` (reads `.private/okeval_run/warehouse.duckdb`, which can be days old) |
| Terminal questions | this machine, `.venv` | `./ask.sh "..."` |
| Evaluation | this machine, `.venv` | `.venv/bin/python -m sf_assistant.evaluation run ...`; one knowledge file: `... evaluation knowledge ...` (only when the owner asks) |
| Model (vLLM, `localhost:8000`) and sync-worker | the `sf-local-ai` stack | **not part of this project** |

## Rules

- **Always name this compose project:** `docker compose -p sf-assistant -f web/compose.yaml ...`, or `web/run.sh`.
  A bare `docker compose stop` in the repository root would stop the `sf-local-ai` stack.
- **Never stop, restart or remove anything of the `sf-local-ai` stack** from here, and never open
  `/data/warehouse.duckdb` (the sync-worker's writer file). `run.sh` has deliberately no `down`.
- **Never delete, truncate or move** `.private/web_log.jsonl`, `.private/mcp_log.jsonl`, the folder
  `.private/ask_logs/` or a file in it, `.private/codes.json`, `.private/mcp_token.json`, `.private/ask_token.json` or
  `.private/mcp_audit_key`. Nothing rotates the page's and the MCP server's logs; the ask service writes one log file
  per business day and deletes only its own daily files past 90 days ([The ask log](#the-ask-log),
  [data_and_privacy.md](data_and_privacy.md#logs-and-kept-files)). The one exception is the one-time split of the old
  single file `.private/ask_log.jsonl`, with the service stopped, by `scripts/split_ask_log.py` ([The ask
  log](#the-ask-log)). They live on the host in `.private/`, which is
  bind-mounted, so recreating a container does not touch them. Never replace `mcp_token.json` with another file
  (`mv`, an editor that saves by renaming): the MCP container mounts that one file and would refuse every token
  until `./run.sh restart-mcp`. `new-mcp-token` rewrites it in place. The same holds for `ask_token.json`, which the
  ask container mounts, and `new-ask-token`.
- **Run `new-codes`, `new-mcp-token` and `new-ask-token` only in the owner's own terminal**, never from a script or an
  agent session: their output holds the plain secrets. All three refuse to run when their output is not a terminal (a
  pipe, a file), and then change nothing.
- **No model evaluation during development** (owner rule, 2026-10-15). Run an evaluation only when the owner asks,
  usually at the end of the day. Deploy checks use `/api/status`, MCP `tools/list` and record-tool calls only, with no
  model call.
- **People use the page.** A restart signs everyone out and drops a waiting question, so keep downtime to one
  container recreate. Restart when the admin status shows no question running, and tell them.

## The page (sf-assistant-web)

### Start, stop, status

```bash
cd sf_assistant/web
./run.sh up            # docker compose -p sf-assistant up -d --build (creates ../.private, mode 700)
./run.sh status        # container state and the signed-out /api/status
./run.sh logs          # follow the container log
./run.sh restart-page  # restart the container
docker compose -p sf-assistant -f compose.yaml stop sf-assistant-web   # stop the page (start again with ./run.sh up)
```

After a start the pipeline needs a few seconds to load the knowledge files and open the read copy (about 5 s on
2026-10-19; `run.sh up` says up to a minute). Until then the status bar says "Assistant starting". The container restarts by
itself after a reboot or a crash (`restart: unless-stopped`), but not after a `stop`. `./run.sh up` never creates or
prints codes: until the owner runs `./run.sh new-codes`, sign-in answers "No access codes are set up yet".

`./run.sh status` shows `Up (healthy)` when all is well, and `{"role":null,"ready":true,"failed":false,"stuck":false}`.
Signed in, `/api/status` also reports the model, the records, the data time, the queue and the hourly allowance; the
admin also gets the status line of the read copy. The page answers only on its bind address, so `curl` must use
that address (`SFA_BIND`).

### After a change

| Change | Then |
|---|---|
| `web/static/*` | nothing: it shows at the next page load |
| `web/server.py`, `web/pipeline.py` | `./run.sh restart-page` |
| what the page imports: the package `src/sf_assistant`, `prompts/*.md`, `knowledge/objects/*.json`, `knowledge/components/` (the flow components) | `./run.sh restart-page` (and `./run.sh restart-mcp` for the MCP server) |
| `web/compose.yaml`, `web/Dockerfile`, `web/requirements.lock` | `./run.sh up` (rebuilds the image and recreates the container) |

Start-up takes one fixed view of the code and the knowledge: it imports every module the pipeline may need and copies
the knowledge files into a private temp folder in the container. Edits during the day therefore never mix old and new
code in one question; they reach the page at the next restart.

Since 2026-10-20 the page imports this package (`sf_assistant.cli.ask`, the function `python -m sf_assistant ask`
runs) from the sf_assistant folder, mounted read-only at `/srv/sfa` (`SFA_HOME`); before, it imported `ask.py` from
`notebooks/object_knowledge_eval` at `/srv/okeval`. A change to the package, a prompt file or a knowledge file needs
`./run.sh restart-page`.

Rollback to the notebook folder (not needed so far). The files are kept in `.private/rollback_20261020/` (git-ignored,
mode 700): `web_before_switch/` (`compose.yaml`, `server.py`, `pipeline.py`, `Dockerfile`, `run.sh`, the
requirements files and `test_web.py` of before the switch), `private_backup_20261020-061332/` (copies of
`web_log.jsonl` and `codes.json` taken before the switch; never copy them back over the live files, which have grown
since) and `before_review/tree.tar` (the package before the stage-2 review fixes). The old image is tagged
`sf-assistant-web:rollback-20261020`.

1. Copy `web_before_switch/{compose.yaml,server.py,pipeline.py}` back into `web/`.
2. `docker tag sf-assistant-web:rollback-20261020 sf-assistant-web:local`
3. `docker compose -p sf-assistant -f compose.yaml up -d --no-build --no-deps sf-assistant-web`

The old `compose.yaml` also carries the MCP service as it was before the review (the whole `.private` mounted), and
the rollback image has no `mcp` package: leave `sf-assistant-mcp` running as it is (`--no-deps` above) and do not run
`up-mcp` or `restart-mcp` until the current `compose.yaml` is back.

### Page settings

Set them in the environment of `./run.sh up`, for example `SFA_BIND=<LAN address> ./run.sh up`. The
machine's own `SFA_BIND` and `SFA_MCP_BIND` go in `web/.env` (git-ignored, example in `web/.env.example`); the environment wins over it. `compose.yaml`
passes each one to the container (an unset one arrives empty, which means the default below;
`tests/test_web.py` checks that every variable the page reads is passed). A change takes effect at the next
`./run.sh up`, which recreates the page and signs everyone out.

| Variable | Default | Sets |
|---|---|---|
| `SFA_BIND` | `127.0.0.1` | the only address the page listens on (loopback until set, so no other device reaches it; `run.sh up` warns when it is not an address of this machine) |
| `SFA_PORT` | `8600` | port |
| `SFA_ALLOWED_HOSTS` | (none) | extra host names for the `Host` header, comma separated |
| `SFA_TIMEOUT_S` | `240` | seconds per question, then a timeout message |
| `SFA_QUEUE_WAIT_S` | `600` | longest wait for the lock, then "busy, try again" |
| `SFA_RATE_PER_HOUR` | `20` | questions per device per rolling hour |
| `SFA_SESSION_HOURS` | `12` | session cookie lifetime |
| `SFA_COOKIE_SECURE` | (off) | `1` adds the cookie's `Secure` flag, once a TLS proxy is in front |
| `SFA_UID`, `SFA_GID` | the caller's | uid/gid of the container user, so `.private/` files stay the owner's |
| `SFA_RESOLVER` | `on` (empty) | the spelling help, `off` or `on`, for the page and the MCP server ([The spelling help](#the-spelling-help-sfa_resolver)) |

`compose.yaml` sets `SFA_WAREHOUSE=/data/warehouse.read.duckdb`, `SFA_HOME=/srv/sfa`, `PYTHONPATH=/srv/sfa/src`,
`SFA_PRIVATE_DIR=/srv/private` (the web log and the codes) and the `OKEVAL_*` model settings.

The address comes from DHCP. When it changes, start with the new one (`SFA_BIND=<address> ./run.sh up`, and
`SFA_MCP_BIND=<address> ./run.sh up-mcp` for the MCP server on port 8700; both commands warn when the address is not
one of this machine's) and add the firewall rules again for the new address.

### How the page behaves

- One question at a time, because the model server is shared with the rest of the stack. Questions wait in line in
  order, and the page shows how many are ahead.
- The limits count per device: a role (friend or owner) plus the client address it signed in from. Signing in again,
  or a second window on the same device, keeps the same allowance and the same question. A device can have one
  question waiting at a time.
- 20 questions per device per rolling hour. The admin code has an allowance of its own, of the same size.
- `POST /api/ask` only queues a question. The page follows it through `/api/status` and fetches the answer from
  `/api/job`. The server keeps the question and the last answer for 15 minutes, so a reload, a locked phone screen
  or a dropped connection does not lose it. Signing out drops a waiting question and the kept answer. A running
  question cannot be stopped; it ends and is logged.
- At most 1000 characters per question.
- **Follow-ups** (2026-11-16). The page sends its last two answered questions with their answers as `recent_turns`
  (each cut to 1,500 characters, the oldest pair dropped while the request would pass 15,000 bytes). The worker then
  runs `Pipeline.answer_followup`: the ask service's rewrite (`agent/followup.py`, the same rules), the answer to the
  rewritten question with its `Read as:` line first, or the unclear line (`I could not tell what this refers to...`)
  and no other model call. A question complete on its own is answered as typed when the rewrite call fails or the model
  rewords it (`detail.rewrite.answered` = `typed`); the model's own UNCLEAR stays a refusal. With no earlier answer the
  question goes to `Pipeline.answer` unchanged. A `recent_turns` of another shape gets 400. The web log's `ask` line
  says how many turns came (`turns`) and its `detail.rewrite` what the rewrite did and how long its call took
  (`seconds`).
- **A failed record step says so** (2026-11-16). When the route call said RECORDS (or gave no reading because it
  failed, and the record agent read records all the same) or a chart ask sent the question to the records, and the
  record step failed or came back empty, the answer is `The figures for this question could not be computed this
  time...` (kind `none`), never the knowledge answer in its place. The knowledge answer may follow, under `What the
  knowledge files say (field meanings only, no record figures):` and without its own Source line, only when its first
  paragraph names a custom object or custom field that a successful data call read (a `describe_fields` lookup or a
  failed call does not count). A draft that the cleaners would cut to its Source line gets one more round first
  (`RECORDS_FINAL_FORM_MESSAGE`), and an empty answer's raw text goes to the private log (`record_dropped_raw`).
- **A timed-out question keeps running.** A Python thread cannot be stopped from outside, so after the 240 s
  timeout the question runs on until the pipeline returns, and it holds the lock until then. Each model call has its
  own timeout (120 s per attempt, up to 3 attempts), so it does end, but that can take several minutes. The admin
  status line shows "A question is still running past its timeout", and the web log records `timeout` and later
  `late_finish`.
- The Source line under an answer gives the sync time of the tables the answer used; the data timestamp is the
  newest sync of the whole copy. The two can differ by a few minutes.
- Sessions live in memory: after a container restart everyone signs in again.

### Health and troubleshooting

The container health check (`server.py --health`, every 60 s) fails when `/api/status` does not answer, when the
pipeline could not be built (`failed`), or when a question has been running more than 30 minutes past its timeout
(`stuck`). Docker shows the container as unhealthy but does not restart it.

| Symptom | Look at | Do |
|---|---|---|
| "Assistant starting" for more than a minute | `./run.sh logs` | wait for the start-up line, or read the error |
| `failed: true` | `./run.sh logs` (start-up error, for example an invalid knowledge file or the BM25 fallback) | fix it, then `./run.sh restart-page` |
| `stuck: true` | admin status: "still running past its timeout" | wait, or `./run.sh restart-page` (signs everyone out) |
| answers say records are off | admin status: records error | check that `/data/warehouse.read.duckdb` exists (the sync-worker writes it); the page tries again before the next question |
| model not ok | `/api/status` (signed in) | vLLM belongs to `sf-local-ai`: tell the owner, do not restart it from here |
| page does not answer | `ss -ltnp \| grep 8600`, `ip -4 addr` | the only line should be `<SFA_BIND>:8600`; after a DHCP change start with the new `SFA_BIND`; check ufw |
| "No access codes are set up yet" | `.private/codes.json` | the owner runs `./run.sh new-codes` |
| the first answer after a quiet hour is slow | `grep VmSwap /proc/$(pgrep -f "python /srv/web/server.py")/status` | the host is short of memory and swaps the idle page out (about 0.5 GB on 2026-10-20); it comes back by itself. Not changed: a container memory limit without swap could stop the page when a question needs more. With `SFA_RESOLVER=on` the page needs about 100-120 MB more ([The spelling help](#the-spelling-help-sfa_resolver)), and the first question after each read-copy swap waits about 2 s more |

### Where the seconds of an answer went

Every answer since 2026-10-29 keeps its timeline (latency logging; `src/sf_assistant/agent/timeline.py`) in the
JSONL record: in the page log's `ask` line (`detail.jsonl.timeline`), in an `ask --file` sidecar and in the sidecars
of an evaluation run. It holds one entry per stage, model call, tool call and check, each with its start (`at`,
seconds from the start of the answer) and its seconds (`s`):

- **stages**: `spelling`, `triage` (with `people_spots`, `route`, `names`, `value_spots`), `records` (with `hints`
  and its size in characters, and `post`: the code after the record agent), `lean` or `knowledge`, `wait_records`
  (the main thread idle until the record agent ends), `merge` (about 0 s and no model call when code merges a lean
  answer: [How answers are merged](#how-answers-are-merged-sfa_merge)), `assemble` (the answer shown: the answer
  choice, the contact rule, the lookup footer and the code's lines);
- **model calls**: the round, what the call answered (`question`, `tools` or `nudge`), the tools offered, max tokens,
  tokens in and out, the finish reason (`length` means the reply was cut at max tokens) and the attempts, with each
  failed attempt (the exception class, the HTTP status, when it failed);
- **tool calls**: the tool, the characters fed back to the model and whether they were cut at 16,000, failed or not
  run, and the earlier call of the same tool it repeats or narrows; each record query in `queries[]` also has its
  `seconds` and the `sql_seconds` of the statements that ran in this answer;
- **checks**: the draft checks of the record agent, the tools-first nudge and the forced answer at the step cap, with
  whether one more model call followed.

The page also logs, on the ask that paid for them, `detail.reopen` = `{close_s, open_s, s}` when the records were
reopened before it, `detail.retry_s` after a swap retry, and `detail.failed_reopen` = `{close_s, open_s, s}` when the
reopen after a swap error failed; `records_reopened` and `records_open_failed` lines carry `close_seconds`. A question
sent with earlier turns also logs `detail.rewrite.seconds`, the follow-up rewrite call. Page seconds = time in line
(`queue_wait_s`, before) + `rewrite.seconds` + `reopen.s` + `retry_s` + the answer's seconds + `failed_reopen.s` + the
hand-off back to the page.

Read it with one command (no model call; it prints only numbers, stage and tool names, fixed words, UTC times and
ticket numbers, so the output can be pasted):

```bash
.venv/bin/python -m sf_assistant.evaluation timeline                        # the last answer of .private/web_log.jsonl
.venv/bin/python -m sf_assistant.evaluation timeline --ticket 7             # page ticket 7 (tickets restart at each start)
.venv/bin/python -m sf_assistant.evaluation timeline --at 2026-10-29T09:14  # the answers that started in that UTC minute
.venv/bin/python -m sf_assistant.evaluation timeline --slowest 10
.venv/bin/python -m sf_assistant.evaluation timeline --stats                # medians per stage, model-call stage and tool
.venv/bin/python -m sf_assistant.evaluation timeline .private/runs/<run>.jsonl --last 3   # an ask --file sidecar
```

The footer of a waterfall names the long pole (the records branch or the knowledge step), how long the main thread
waited, the net prompt growth per round (shown only between calls that offer the same tools), the rounds that checks
cost, repeated and narrowed tool calls, a merge that was not used, and the code outside every entry. The calls are
not streamed, so a model call's seconds are its whole time (waiting at the server, reading the prompt and writing the
reply together). The first route and names calls after a reopen also build the session's model client. Answers
logged before 2026-10-29 show their stage totals only. A timeline adds about 3 KB to an `ask` line.

## The MCP server (sf-assistant-mcp)

The second service in `web/compose.yaml` uses the same image, `network_mode: host`,
`restart: unless-stopped` and a health check. It mounts `src/`, `prompts/` and `knowledge/` read-only, the data
volume read-only at `/data`, and of `.private/` only its own three files: `mcp_token.json` and `mcp_audit_key`
read-only, `mcp_log.jsonl` read-write. It never sees the web log, the codes, the question sets, the run folders or the
snapshot copy. `run.sh up` and `up-mcp` create the three files first (a file mount needs them).

It listens on `SFA_MCP_BIND:SFA_MCP_PORT` (default `127.0.0.1:8700`) with Streamable HTTP at `/mcp`, and every
request needs the bearer token. It is read-only and takes record data only from the read copy. It offers the
knowledge tools, the 14 record tools, the resources `sfa://object/{api}` and `sfa://freshness`, and the answer
templates as prompts. Each client may make 30 tool calls a minute (`SFA_MCP_RATE_PER_MIN`) and 120 resource reads
and prompt gets.

With `SFA_CHARTS=on` at the server's start, the default since 2026-11-04, `group_records`, `trend_records` and
`compare_records` take an optional `chart: true`. The tool runs as usual, `result` stays what it is, and the envelope gets one more key, `chart`: the
spec, the rows, the notes and the SVG as JSON text, or a null `chart` with the plain reason and any rows. A failed call
gets no such key, and any value but `true` gets no chart. There is no new tool and no type option, so the tool counts
stay. With the switch off, the listing and the envelopes are as before and `chart` is an unknown argument
(E11a). The server reads the switch once at start, so a change needs `./run.sh restart-mcp`. Read what this sends to
the client's model first: [Charts](#charts-sfa_charts), "On MCP".

```bash
ss -ltnp | grep 8700                       # before the first start: the port must be free
cd sf_assistant/web
./run.sh new-mcp-token                     # owner's own terminal only: prints the token once
./run.sh up-mcp                            # build the image and start only the MCP service (the page is not recreated)
./run.sh mcp-status                        # container state and /healthz (no token needed, nothing about the data)
docker compose -p sf-assistant -f compose.yaml ps
docker compose -p sf-assistant -f compose.yaml logs -f --tail=200 sf-assistant-mcp
docker compose -p sf-assistant -f compose.yaml restart sf-assistant-mcp
docker compose -p sf-assistant -f compose.yaml stop sf-assistant-mcp
```

Name the service in these commands so the page is not recreated. `./run.sh up` starts both services. The MCP service
has no build section of its own: `docker compose ... up -d --build sf-assistant-mcp` would not rebuild the image, so
use `./run.sh up-mcp` after a change to `web/requirements.lock` or `web/Dockerfile`.

Health (`./run.sh mcp-status`, `/healthz`, the container health check): `{"ok": true, "tools": 35, "records": "on",
"tokens": "ok"}`. `ok` is false while no valid token is set up (`tokens` `missing` or `invalid`: every request gets
503 until `./run.sh new-mcp-token`), while the token file was replaced instead of rewritten (`tokens` `stale`: run
`./run.sh restart-mcp`), or while the read copy cannot be opened (`records` `off`). Docker shows the container as
unhealthy then, but does not restart it.

Deploy check, with no model call: the container is `Up (healthy)`; an MCP client's `tools/list` shows the 35 tools (18 knowledge, 3 flow component, 14 record), and `group_records`, `trend_records` and `compare_records` list `chart`;
one record-tool call (for example `get_sync_status`) answers; `.private/mcp_log.jsonl` gets one line per call; a
request without the token is refused (401). Since no one but the owner may see a token, an agent's check adds a
temporary token in the same process, uses it and removes it again, and never prints it.

Clients (read [MCP clients and record data](data_and_privacy.md#mcp-clients-and-record-data) first: a client on a
cloud model sends record data with real names off this machine, and it may follow instructions planted in record
text):

- **Claude Code**, on any LAN machine the firewall allows:
  `claude mcp add --transport http sf-assistant http://<SFA_MCP_BIND>:8700/mcp --header "Authorization: Bearer <token>"`
- **Claude Desktop** on another LAN machine, through the `mcp-remote` bridge (plain HTTP needs `--allow-http`):

  ```json
  {"mcpServers": {"sf-assistant": {"command": "npx",
    "args": ["-y", "mcp-remote@0.14.3", "http://<SFA_MCP_BIND>:8700/mcp", "--allow-http",
             "--header", "Authorization:${SFA_MCP_AUTH}"],
    "env": {"SFA_MCP_AUTH": "Bearer <token>"}}}}
  ```

- **stdio on this machine, live data**: command `docker`, arguments `exec -i sf-assistant-mcp python -m
  sf_assistant.server --stdio`. It needs no token and logs as client `stdio`, so anyone in the `docker` group can use
  it.
- **stdio on this machine, local snapshot**: command `<sf_assistant folder>/.venv/bin/python`, arguments `-m
  sf_assistant.server --stdio`. It reads `.private/okeval_run/warehouse.duckdb`, not the live read copy: refresh it
  first (`./ask.sh --refresh-warehouse`); `sfa://freshness` and `get_sync_status` show its age.

Put the real token only into the client's configuration, never into a file in the repository.

## The ask service (sf-assistant-ask)

**A release folder does not follow the main tree.** When the service runs from a release copy, its code and `knowledge/` change
only when you rebuild, copy them to the release folder and restart. Page results stop proving the release once the main tree
moves.

One MCP tool, `ask_salesforce`, that runs the full pipeline, for the platform's `/chat`
([architecture.md](architecture.md#the-ask-service) holds the contract). It is its own compose project
(`web/compose.ask.yaml`, project and container `sf-assistant-ask`), so starting or stopping it touches neither the page
nor the MCP server, which it shares only the image with. **State on 2026-11-04: built and checked on a dead model
address, not started, and no client calls it.** Connecting the orchestrator and any real question through the model
are later stages, on the owner's word.

**It has no TCP port.** It listens only on the Unix socket `/run/sf-assistant-ask/ask.sock`, mode 666, inside the named
volume `sf-local-ai_ask-sock`, so only a container with that volume can reach it. A client asks for
`http://sf-assistant-ask/mcp` on that socket (the only Host value it accepts), with the bearer token. Any user in a
container with the volume can connect. **The orchestrator runs as root** (its Dockerfile has no `USER`, its compose no
`user:`), and it must mount the volume `:ro`: with a read-write mount it could remove or replace the socket. It keeps
`network_mode: host`, because the record guard accepts only a local model address (`localhost:8000`, pinned in the compose
file), and it refuses to start when `urllib.request.getproxies()` holds a proxy for `http`, `https` or `all` (any spelling
of the variable: Python and the model client read them in any case), because that guard reads the URL, not where the client
sends it.

**The volume is the platform's.** The compose file declares it `external: true`, the platform's compose creates it (stage
2), and `up-ask` stops with a message when it is missing. It must be writable by the uid of the image's user (1000). A
plain new volume is root's: `up-ask` still prints success, because it only checks that the volume exists, and the container
then loops on exit 2 with a clear message in its log (`cannot create the socket ...`). The form checked on 2026-11-04 is a
tmpfs volume, which leaves no stale socket after a reboot:

```yaml
volumes:
  ask-sock:                       # project sf-local-ai: the volume sf-local-ai_ask-sock
    driver_opts: {type: tmpfs, device: tmpfs, o: "uid=1000,gid=1000,mode=0755"}
```

It mounts `src/`, `prompts/`, `knowledge/` and `web/` read-only, the data volume read-only at `/data`, and of `.private/`
only `ask_token.json` read-only and the log folder `ask_logs/` read-write ([The ask log](#the-ask-log)), with
`mem_limit: 4g` and `pids_limit: 512`. Its container log is capped at 3 files of 10 MB. It reads the
code and the knowledge of the folder it is started from, once, at its start: a change needs a restart, and a copy of
the folder does not follow the folder it came from.

```bash
docker volume inspect sf-local-ai_ask-sock    # before the first start: the platform's compose has created it
cd sf_assistant/web
./run.sh new-ask-token --label orchestrator   # owner's own terminal only: prints the token once
./run.sh up-ask                               # starts only this service; it never builds the image or creates the volume
./run.sh restart-ask                          # after a code, prompt or knowledge change: the same checks, then recreates only this service
docker compose -p sf-assistant-ask -f compose.ask.yaml ps
docker compose -p sf-assistant-ask -f compose.ask.yaml logs -f --tail=200 sf-assistant-ask
docker exec sf-assistant-ask python /srv/web/ask_mcp.py --health   # exit 0 when /healthz says ok
docker stop sf-assistant-ask                  # the fast off: the socket goes away; "unless-stopped" keeps it down
```

A stop ends the process in about 5 s also with a question running: the thread cannot be joined, so the service exits
without it (under docker's 10 s to SIGKILL). The running question's answer is lost, and a waiting one is logged
`abandoned`. uvicorn logs a traceback of framework frames there, with no text of a question. At its start the service
replaces a stale socket, refuses (exit 2) a socket something answers on or that gives no answer in 2 s, and at its stop it
removes the socket only if the path still holds its own.

`up-ask` never builds `sf-assistant-web:local`, because the page and the MCP server run on that image. Without the image
it says so and stops; `./run.sh up` builds it. A new `web/requirements.lock` therefore reaches this service only after
the page's project has rebuilt the image, and then through `./run.sh up-ask` (a plain restart keeps the old image).

**Copy `agent/followup.py` and `prompts/followup_rewrite.md` together, always.** The page and the service import every
module of `sf_assistant.agent` at their start, and that module reads its prompt file at import. `followup.py` without its
prompt file (the page's folder, a hand copy) stops the page at its next start.

How it behaves:

- **One question at a time**, FIFO. A second caller waits up to 120 s, then gets `busy`. A question gets 240 s, then
  `timeout`, but a thread cannot be killed: it keeps running and holds the worker until it ends, up to about 28 minutes
  with a hung model (an estimate: 3 attempts of 120 s per call), and callers get `busy` meanwhile. `/healthz` shows
  `"late": true` for that time and the log gets a `late_finish` line. A caller that goes away is not noticed: a queued
  question still runs at its turn. A client must wait at least 370 s, because a JSON reply sends no byte until it is
  done.
- **A follow-up is rewritten first** when the call brings `recent_turns`, with one short call on the main model
  (`agent/followup.py`, `prompts/followup_rewrite.md`). With no turn there is no call and the question is unchanged. Code
  reads the reply and refuses what it can (`status` `unclear`; the log's `reason` says which rule and `rewrite_reply` holds
  the model's words, to diagnose an honest refusal). A user turn is trusted more than an assistant turn, where record text
  arrives. Code cannot catch every rewrite: the field block and the contact rule still apply to the question that runs,
  `Read as:` shows it, and only admins reach the service.
- **The read copy can break while the rewrite runs:** the knowledge step then makes its model calls with no record
  session. No record is read, and the status stays `failed`.
- **A fault never shows text.** The caller gets `failed`, the log gets the class and the reason, and the container log
  gets start-up lines and class names only. A fault in the log itself costs the line, never the answer.
- **The page and this service share no lock**, so two questions can reach vLLM at once. Neither goes through the
  platform's admission lanes.

Health (`/healthz` on the socket, the container health check): `{"ok": true, "tools": 1, "records": "on", "tokens":
"ok", "late": false}`. `ok` is false while no valid token is set up (`tokens` `missing` or `invalid`: every request gets
503, and that text names `./run.sh new-ask-token`), while the token file was replaced instead of rewritten (`tokens`
`stale`: restart the container) or while records are off (`records` `off`: every question gets `failed` with no model
call). **The recovery delay:** `records` goes `off` as soon as the read copy's file is gone, and comes back only at the
next question that reopens it, because nothing reopens between questions. A new file reopens at that question; a failed
open on an unchanged file is tried again after 60 s (`REOPEN_RETRY_S`). So `/healthz` can stay `off` with a good file
until a question arrives, and that question is answered.

Check with no model, as on 2026-11-04: a compose override with a separate project and container name, a dead model
address (`OKEVAL_BASE_URL=http://127.0.0.1:9/v1`), a throwaway socket volume, and a throwaway token and log in a scratch
folder, removed afterwards and never printed. A second throwaway container, sharing only that volume, with no network,
reads `/healthz` and gets 401 with no token: as root with the volume `:ro`, as the orchestrator will, and as uid 10001. It
must log `pipeline ready` in about 10 s and answer `failed` or `no_answer` with empty answer text. Never run it against
`localhost:8000`: that is a model call on the shared server.

### The ask log

**From the release that ships it** (option R2 of the conversation-memory design of 2026-11-25, owner decision D5;
`web/ask_log.py`, `web/compose.ask.yaml`, `web/run.sh`, `scripts/split_ask_log.py` and the `web/ask_mcp.py` that writes
through `RotatingAskLog`, in one release and one `restart-ask`): the approved retention keeps the ask log 90 days,
rotated daily. A file bind mount cannot be rotated by a rename (the container keeps writing into the
renamed file, as with the token files), so the service now gets a folder and picks the file of each line itself.

| What | How |
|---|---|
| Folder | `.private/ask_logs/`, mode 700, created by `./run.sh up-ask` and `restart-ask` (a link there is refused); mounted read-write at `/srv/sfa/.private/ask_logs` (`web/compose.ask.yaml`), the container's only writable host path |
| Files | `ask_log-YYYY-MM-DD.jsonl`, mode 600, one per business date in **Asia/Kolkata** (`web/ask_log.py`, `RotatingAskLog`). `ts` keeps its old form, the container's local time (UTC) with its offset: `2026-11-25T18:30:00+0000` is 00:00 on 2026-11-26 in Kolkata, so that line is in the file of 2026-11-26 |
| Lines | the same lines as before (one JSON object per line, `ts` first), each one `O_APPEND` write under the thread lock; a link named like a daily file is never written through |
| Kept | 90 days: `SFA_ASK_LOG_DAYS` (a whole number from 1 to 3650; empty or unset: 90; any other value keeps 90 and says so once in the container log). At its start and at the first write of each business day the service deletes the files named exactly `ask_log-YYYY-MM-DD.jsonl` whose date is before today minus 90 days: on 2026-11-25 it keeps 2026-08-27 to 2026-11-25, so a line is kept 90 to 91 days while the service writes every day. It runs no timer: a service that writes nothing deletes nothing until its next start or write, so a file is kept **at least** 90 days and is deleted at the first start or write after that. Only names in ASCII digits count. It deletes nothing else (not `ask_log-unknown-date.jsonl`, no other name, no link, no folder). The container log gets the count and the date of a deletion, nothing more. Changing the 90 days is the owner's decision |
| Container log | `json-file`, at most 3 files of 10 MB (`logging` in `web/compose.ask.yaml`; before that release it had no cap) |
| Faults | a line that cannot be written costs that line only, and the container log gets `could not write the log: <class name>`; at its start the service refuses (exit 2) a folder it cannot write to |

Read the days in order (their names sort by date):

```bash
cat .private/ask_logs/ask_log-2*.jsonl | jq -c '{ts, event, status, reason}'   # every day; not the unknown-date file
jq -c 'select(.event == "ask")' .private/ask_logs/ask_log-2026-11-25.jsonl     # one day
```

**The one-time split of the old file.** Before that release the service wrote one file, `.private/ask_log.jsonl`.
`./run.sh up-ask` and `restart-ask` start nothing while that file is there. Split it once, in the `sf_assistant` folder
the service runs from (the release folder when it runs from one, never another copy), **with the service stopped**, so
no line lands in the old file after the split. The new `web/compose.ask.yaml`, `web/run.sh`, `web/ask_log.py`,
`scripts/split_ask_log.py` and the `web/ask_mcp.py` that writes through `RotatingAskLog` go to that folder together: an
older `ask_mcp.py` finds no writable log file in the new container (its root is read-only) and exits 2 at its start, and
the new one finds no folder under the old compose file. `./run.sh up-ask` and `restart-ask` check the first half: they
start nothing (exit 2) while the `web/ask_mcp.py` next to them does not write through `RotatingAskLog`.

```bash
cd <the sf_assistant folder the service runs from>
docker stop sf-assistant-ask                              # 1. stop: nothing writes the old file any more
python3 -I scripts/split_ask_log.py                       # 2. dry run: lines per business day; writes nothing
python3 -I scripts/split_ask_log.py --apply               # 3. the split; the old file becomes the backup
ls -la .private/ask_logs .private/ask_log.legacy.jsonl    # 4. drwx------ and -rw------- only; no .private/ask_log.jsonl
(cd web && ./run.sh restart-ask)                          # 5. start on the folder mount
```

What the split does (`scripts/split_ask_log.py`; its docstring has the details):

- Each line goes, in its order, to the daily file of its `ts`'s business date. A line with no readable `ts` (a blank
  line, a line cut before its `ts` ended, a `ts` with no time zone) goes to `ask_log-unknown-date.jsonl`. No line is
  dropped or changed; a last line with no newline gets one.
- When every file reads back as written, the old file becomes the backup `.private/ask_log.legacy.jsonl` (mode 600: a
  hard link, then the old name is removed). The service never sees the backup and never deletes it. The split prints
  the day from which the backup, and `ask_log-unknown-date.jsonl` if it made one, may be deleted: the day the 90-day
  rule deletes the daily file of the backup's newest line (that date plus 91 days). Delete them by hand from that day:
  `rm .private/ask_log.legacy.jsonl .private/ask_logs/ask_log-unknown-date.jsonl`.
- Daily files dated before the 90 days are written too; the service deletes them at its start, and the dry run counts
  them.
- **A folder that already holds log files** (a daily file or the unknown-date file, for instance because the new service
  ran before the split) is refused: exit 2, nothing written. With `--append` each day's lines are added at the end of
  that day's file with `O_APPEND` and nothing there is rewritten, so no line already there is lost; that day's split
  lines then follow its newer lines, and every line keeps its own `ts`. A backup name that exists is never replaced:
  name another with `--legacy`.
- Without `--append` the daily files are created new. On any fault, or when the old file changed during the split (the
  service still running), the files this run made are removed, the old file stays as it was, and the exit code is 1.
- It prints names, dates, counts and sizes, never a line's text.

**Rollback:** stop the service, put back the earlier `web/compose.ask.yaml`, `web/run.sh` and
`web/ask_mcp.py`, and run `./run.sh restart-ask`: the earlier `run.sh` creates an empty `.private/ask_log.jsonl` and lines
go there again. The daily files stay readable, and nothing deletes them while rolled back. To go forward again: stop,
`python3 -I scripts/split_ask_log.py --apply --append --legacy .private/ask_log.legacy-2.jsonl`, restart.

### The memory switches

Salesforce conversation memory (design v2 of 2026-11-25, §15) has six switches in the ask service. What each mode does
is in [architecture.md](architecture.md#configuration) ("Contract v2"); in short, `off` is today's contract v1 with
no state, `shadow` answers byte for byte as `off` and also returns the next query state to a contract-v2 caller, and
`on` runs the follow-up resolver and the stored plan.

| Switch | Unset or empty | Accepted values |
|---|---|---|
| `SFA_MEMORY` | `shadow` | `off`, `shadow`, `on` |
| `SFA_MEMORY_SHORTCUTS` | `on` | `off`, `on` (used with `SFA_MEMORY=on` only) |
| `SFA_MEMORY_PATCH_KNOWLEDGE` | `off` | `off`, `on` (owner decision D4: off) |
| `SFA_MEMORY_MAX_CALLS` | `3` | a whole number from 1 to 3 |
| `SFA_MEMORY_MAX_AGE` | `2` | a whole number from 0 to 6 |
| `SFA_MEMORY_TOKENS` | `12000` | a whole number from the resolver's smallest prompt (`memory_tokens_floor()`, about 5,400) to 100000 |

How they reach the service:

- `web/compose.ask.yaml` passes each one as `"${NAME:-}"`, as it passes `SFA_RESOLVER` and `SFA_ASK_LOG_DAYS`: the
  value comes from the shell that runs `./run.sh` (exported, or typed in front of the command), or else from the
  git-ignored `web/.env` (a value in the shell wins over it). Unset or empty is the code default in the table
  (`src/sf_assistant/config.py`, `memory_settings`).
- The service reads them once, at its start. `./run.sh restart-ask` recreates the container and takes the values of
  that moment; `docker restart` and a reboot keep the values the container was created with.
- **A value typed in front of the command lasts for that one start.** The next `restart-ask` or `up-ask` without it
  (after a code, prompt or knowledge change, for instance) starts on the defaults again, so `on` would quietly become
  `shadow`. A setting that must stay goes in `web/.env`. `web/compose.yaml` (the page and the MCP server) passes none
  of these names, so a line there changes the ask service only.
- A value the code cannot read never stops the service: `SFA_MEMORY` is then `off` (contract v1), any other switch
  keeps its default, and the container log gets one line that names the switch and what it must be (never the value).
  Unlike `SFA_RESOLVER`, `run.sh` does not check these values first.
- They live in `web/compose.ask.yaml`: putting back a compose file older than the one that passes them (for instance
  for the ask log's rollback above) drops them, and the service runs the defaults (`shadow`) whatever the shell holds.

Which mode the service started in is in the start-up line of the ask log (fixed words, no question text). The service
writes that line once, at its start, into the daily file of that day, so the check reads every daily file and keeps the
last start-up line:

```bash
cd <the sf_assistant folder the service runs from>/web
awk 1 ../.private/ask_logs/ask_log-2*.jsonl \
  | jq -cR 'fromjson? | select(type == "object" and .event == "startup") | {ts, ok, memory, memory_problem}' | tail -n 1
docker logs --tail=50 sf-assistant-ask       # one line here when a switch was refused
```

- `memory` is the mode of that start, and `ts` is its time in UTC (the container's clock).
- `"ok":false` (with `"memory":null`) means the start failed, and the service is not answering.
- A line cut by a crash does not hide a later start: `awk 1` ends a file's cut last line, so it is not joined to the
  next file's first line, and `jq -R` with `fromjson?` skips a line that is not whole JSON instead of stopping at it.
- Nothing printed means the service has not started since the daily files began, or the file of its start day is
  older than `SFA_ASK_LOG_DAYS` and was deleted. A restart writes a new line.

The rollout steps that touch the ask service (design §15.2), each on the owner's word only. Each step sets the switch,
checks that no value in the shell would win over the file, restarts, and then reads the new start-up line. The line
comes once the pipeline is built, which takes about 10 s and at most a minute. Until then the check still shows the
previous start (an older `ts`), so run it again.

Step 1, the release: none of the six switches in `.env` or in the shell, so every switch has its default (`shadow`).

```bash
cd <the sf_assistant folder the service runs from>/web
grep '^SFA_MEMORY' .env; env | grep '^SFA_MEMORY'   # both print nothing
./run.sh restart-ask
sleep 15                                            # the start-up line comes once the pipeline is built
awk 1 ../.private/ask_logs/ask_log-2*.jsonl \
  | jq -cR 'fromjson? | select(type == "object" and .event == "startup") | {ts, ok, memory, memory_problem}' | tail -n 1
# expect "ok":true and "memory":"shadow", with the ts of this start
```

Step 5, only after the model evaluation of the chains that the owner asks for: `on`, kept across later restarts. The
first line changes an `SFA_MEMORY=` line of the git-ignored `.env`, or adds one on a line of its own when there is
none. The `\n` in front ends a last line that has no final newline. A plain `echo ... >> .env` would join the switch to
that line, so the service would stay in `shadow` and the setting on that line (for example `SFA_BIND`) would break.

```bash
cd <the sf_assistant folder the service runs from>/web
grep -qs '^SFA_MEMORY=' .env && sed -i 's/^SFA_MEMORY=.*/SFA_MEMORY=on/' .env || printf '\nSFA_MEMORY=on\n' >> .env
grep '^SFA_MEMORY=' .env; env | grep '^SFA_MEMORY'   # SFA_MEMORY=on, once; then nothing: a shell value would win
./run.sh restart-ask
sleep 15                                            # the start-up line comes once the pipeline is built
awk 1 ../.private/ask_logs/ask_log-2*.jsonl \
  | jq -cR 'fromjson? | select(type == "object" and .event == "startup") | {ts, ok, memory, memory_problem}' | tail -n 1
# expect "ok":true and "memory":"on", with the ts of this start
```

The rollback (design §15.3): `SFA_MEMORY=off` with `restart-ask` gives today's behaviour, contract v1 for every call
and no state. The platform's bridge then gets v1 replies and stores no row of its own for them. One exception: with
`SF_ASSISTANT_MEMORY=true`, a failed turn that directly follows a stored row still copies that row (the retry line
keeps its plan), and the service ignores that state while it is off. `SFA_MEMORY=off ./run.sh restart-ask` works
at once, but the next `restart-ask` reads `web/.env` again, so change the line there too. If `env` prints an
`SFA_MEMORY` line, that value wins over the file: `unset` it in this shell first.

```bash
cd <the sf_assistant folder the service runs from>/web
grep -qs '^SFA_MEMORY=' .env && sed -i 's/^SFA_MEMORY=.*/SFA_MEMORY=off/' .env || printf '\nSFA_MEMORY=off\n' >> .env
grep '^SFA_MEMORY=' .env; env | grep '^SFA_MEMORY'   # SFA_MEMORY=off, once; then nothing: a shell value would win
./run.sh restart-ask
sleep 15                                            # the start-up line comes once the pipeline is built
awk 1 ../.private/ask_logs/ask_log-2*.jsonl \
  | jq -cR 'fromjson? | select(type == "object" and .event == "startup") | {ts, ok, memory, memory_problem}' | tail -n 1
# expect "ok":true and "memory":"off", with the ts of this start
```

Back to `shadow`: the same first line with `shadow` in place of `off`, then the same checks, expecting
`"memory":"shadow"`. The platform's own switches (`SF_ASSISTANT_MEMORY`, `SF_ASSISTANT_SUMMARY`) are separate: they
are orchestrator settings (`docs/CONFIG.md` at the repository root) and need an orchestrator restart.

## Firewall

Check the host firewall before anyone else uses the page. Until `sudo ufw status` shows the rules below, assume that
**every device on the LAN can reach the page's and the MCP server's ports** when they listen on a LAN address. The page
still needs its access code and the MCP server its token, but both travel as plain HTTP.

The containers use the host network and publish no Docker port, so ufw rules apply directly. The owner runs these
himself, in this order:

```bash
sudo ufw status verbose            # inactive? then, before enabling (its default policy drops incoming traffic):
sudo nft list ruleset              # anything else filtering already?
sudo ss -ltnp                      # every listening service; allow each one LAN users or other hosts need
sudo ufw allow OpenSSH             # or: sudo ufw allow <ssh port>/tcp  -- first, or the next step locks you out
# a page user's device (use its fixed or DHCP-reserved LAN address)
sudo ufw allow from <user's address> to <SFA_BIND> port 8600 proto tcp comment 'sf-assistant: page user'
# your own laptop or phone, if it is not this machine
sudo ufw allow from <your address> to <SFA_BIND> port 8600 proto tcp comment 'sf-assistant: owner'
# an MCP client (only the client's address)
sudo ufw allow from <client's address> to <SFA_MCP_BIND> port 8700 proto tcp comment 'sf-assistant: mcp'
sudo ufw enable
sudo ufw status numbered
sudo ufw delete <number>           # remove a rule
```

Put in the real addresses. **Hand out an MCP token only after this is done.** While
`/etc/ufw/ufw.conf` says `ENABLED=no`, `./run.sh up`, `up-mcp` and `new-mcp-token` print a warning on stderr (nothing
else changes). Until then,
either stop the MCP service (`docker compose -p sf-assistant -f web/compose.yaml stop sf-assistant-mcp`) or bind it to
this machine only (`SFA_MCP_BIND=127.0.0.1 ./run.sh up-mcp`) and use stdio or an SSH tunnel.

## Rotate the codes and the token

```bash
cd sf_assistant/web
./run.sh new-codes        # new access code + admin code, printed once; the old ones stop working at once
./run.sh new-mcp-token    # new bearer token, printed once; the old one stops working
./run.sh new-ask-token    # the ask service's own bearer token, printed once; the old one stops working
```

Rotate the page codes when a user no longer needs access or a code may have been seen. Every signed-in session
ends at its next request. Give the user the new access code in person or by a private message.

The MCP token: only the owner runs `./run.sh new-mcp-token`. `--add` adds a token for one more
client and keeps the others; without it, every older token stops working. Rotate it when a client no longer needs
access or the token may have been seen, and put the new one into each client.

The ask service's token works the same way and only on that service, and an MCP server token gets 401 there. Its
client is the orchestrator, so a new one also goes into the orchestrator's secrets.

## Ask from the terminal

```bash
./ask.sh "How do I stop the background check reminders?"
.venv/bin/python -m sf_assistant ask "How do I stop the background check reminders?"   # the same
./ask.sh -v "..."                    # also print every tool call and result
./ask.sh --records-only "..."        # record agent only
./ask.sh --no-records "..."          # knowledge answer only (no duckdb)
./ask.sh --today 2026-10-13 --tz America/New_York "..."   # pin today and the business time zone
./ask.sh --file .private/questions/records.txt            # every question in a file: transcript + .jsonl in .private/runs/
./ask.sh --help
```

Each question makes model calls. The cli reads the local snapshot `.private/okeval_run/warehouse.duckdb`. It warns
when the snapshot is older than 24 hours. Refresh it with:

```bash
./ask.sh --refresh-warehouse         # one docker cp of /data/warehouse.read.duckdb from sf-local-ai-orchestrator-1
```

The copy is checked read-only and replaces the old snapshot atomically. Only this folder's copy changes; the notebook
folder keeps its own, so from then on the two folders read different data. For any comparison of the two, pass one
file to both sides (`--warehouse`, `OKEVAL_WAREHOUSE`, `SFA_LIVE_SNAPSHOT`) or check that both sha256 are equal
first.

Under the answer (the terminal and every `--file` transcript), a question whose route call said `NO_RECORDS` or was
unclear, but that code sent to the records, shows why as the first line of the record trace:
`-- route: the route call said NO_RECORDS; overruled by the stored value '<words>' (<Object>.<Field>, <rule>)`, or
`... overruled by a named person`. The JSONL row's `triage` carries `values` (the stored record values the question
names, without the stored values themselves), `overruled_by` and `overrule_reason`; the page's admin trace data
carries the same as `route` (the admin panel does not display it; the route line is in the transcript;
[value_spots.md](value_spots.md)). Opening the record session builds the value spots' entity index too (0.6 to 1 s and
20 to 45 MB on the local snapshot; the first question of a session about 0.3 s more). The start-up lines gain
`value index cut: ...` only when a limit cut it.

## The spelling help (SFA_RESOLVER)

The term resolver ([resolver.md](resolver.md)) reads misspelt object, field, value and person names ("accont",
"Completd", "Dellta Persn"): a clear correction is used and stated (a tool note and one `Read as: ...` line in the
answer, or the lead sentence for a person), and anything less clear becomes a did-you-mean. The switch is `on` by
default since 2026-10-27 (the owner's order of 2026-10-26, after the evening evaluation:
[resolver_integration.md](resolver_integration.md) (8)): unset or empty is on, `SFA_RESOLVER=off` turns it off. With
it off nothing changes (parity holds). The parity harness and the evaluation's reference replay of the golden tool
calls always run with it off.

```bash
./ask.sh "how many interviews are Completd"                     # terminal: a start-up line "spelling help on (...)"
SFA_RESOLVER=off ./ask.sh "how many interviews are Completd"    # the same question without the spelling help
.venv/bin/python -m sf_assistant.evaluation run ...             # evaluation runner (owner's run only): on, as the cli
SFA_RESOLVER=off .venv/bin/python -m sf_assistant.evaluation run ...   # an evaluation run without it
cd web && SFA_RESOLVER=off ./run.sh up       # page + MCP server off: rebuild, recreate, sign everyone out
cd web && SFA_RESOLVER=off ./run.sh up-mcp   # the MCP server alone (the page is not recreated)
cd web && ./run.sh up                        # back to the default (on)
```

- It needs an English word list: the system lists (`/usr/share/dict/american-english`, `british-english`; packages
  `wamerican wbritish`, installed by `web/Dockerfile`) or `SFA_LEXICON=<file>[:<file>...]`. Without a readable one
  it stays off and says so.
- The image holds both word lists (`web/Dockerfile`; the running image of 2026-10-22 has them with the benchmarked
  hash, checked 2026-10-27). `restart-page` / `restart-mcp` keep the containers' environment and load the mounted
  code: a container started with an empty `SFA_RESOLVER` follows the code default (on) after a restart; another
  value needs `./run.sh up` or `up-mcp`. Docker compose also reads a git-ignored `web/.env` when there is one (a
  value in the shell wins over it).
- Start-up lines (counts only): `spelling help on (knowledge files): ...` and `spelling help on (knowledge files +
  snapshot): ...` (each ends with `word list <n> words (sha <hash>)`; the hash changes only when the word set does),
  or `spelling help off: <reason>` (records stay on). The page logs the first in its `knowledge_built` line
  (`spelling`) and the second in the `lines` of `records_opened`; the MCP server in its log (its knowledge tools get
  the knowledge resolver too). A value other than `off`, `on` or empty never stops anything: the line says
  `spelling help off: SFA_RESOLVER must be off or on`, and `./run.sh up` / `up-mcp` refuse such a value before
  starting the containers.
- The image build stops when the word lists are not the benchmarked ones (`web/Dockerfile` checks their SHA-256): a
  newer base image with other lists must not change the corrections silently. Re-run the resolver benchmark before
  changing that hash.
- Cost (measured table: [resolver.md](resolver.md#how-to-switch-it-off), "Cost"): the page process uses about
  +95-120 MB with the switch on (start-up and after reopens; peak about +105 MB), `pipeline.build` takes about +1 s,
  and every record session reopen after a read-copy swap about +2 s: the first page question or MCP call after each
  swap waits that much longer.
- The JSONL rows of `--file` runs and the evaluation get `spelling` (the question's misspelt words) and
  `read_as_line`.

## How answers are merged (SFA_MERGE)

A record question in merged mode gets two answers side by side: the record agent's (the numbers, names and dates,
audited by code, ending with its Source line) and the knowledge step's. When the route call says RECORDS, the knowledge
step is the lean one: a facts block of 2 to 5 "- " bullets on what the objects, fields and values mean
(`agent/lean.py`).

Since 2026-10-29 (latency plan step 2, the owner's go: "at most 3 relevant meaning bullets") code merges the two in
lean mode, with no model call (`agent/merge.py`, `code_merge`):

- The record answer stays as it is: its lead, numbers, names, dates and wording.
- At most 3 bullets of the facts block are added, in their order and as the lean step wrote them, after a blank line
  and before the Source line, which stays last. Nothing is rewritten. When the record answer ends with a list (a
  breakdown, a profile), the code line `What the fields mean:` (`prompts/code_merge_label.md`) comes first, so the
  bullets start a list of their own on the page and in Markdown instead of continuing that list.
- A bullet is kept only when it is about a field the answer used, by object and field (review of 2026-10-29): a field
  of a successful record query (`fields_used`, each path hop with its own object; the first name of each path of its
  "applied" block; the lookup of a lookup check), or one the record answer names. A field the bullet puts on an
  object (`Obj.Field`, "`Field` on `Obj`", "`Obj`'s `Field`") counts only as that pair, so a bullet about a field the
  count did not use is left out even when it also names the counted object, and so is one about a field of the same
  name on another object. A bullet that names no field is kept only for the object of a counting query, or one the
  record answer names; never for a table a query only joined. A lookup named after its object ("`Department__c` is
  the employee's department") counts as that field when the count used it. The record catalog's objects tell an
  object from a field.
- People answers (the person tool ran): only bullets about the lookups the answer used are kept: those of the counts
  that ran, else those of the person tool's references and links with records. A bullet that names a person of the
  question (the record session's named people, the triage's names, missing names and spellings, the stored
  spellings) is left out: the lean step reads no record, so what it says about the people is a guess, and in the
  evaluation runs such bullets contradicted the record answer.
- A bullet is also left out when it holds a number that no record result, quoted value or API name of the facts
  block, the question or the status line holds (the merge call's number audit for a facts block); after a name was not
  found, any number beyond the question's (the merge call's not-found rule); when it is working notes or tool talk,
  says how to fetch records, talks about the knowledge, the documentation, the question or the prompt, claims that no
  field links someone, gives advice ("one must", "should be used") or guesses ("would be found", "probably"), repeats
  six words of the lean prompt, is cut off (an open code span, bracket or bold, an unfinished end) or longer than 400
  characters, is only a label or repeats the record answer; and when the merge call's answer would be rejected for it
  (a condition no query applied, an object or field called not available that the warehouse holds, a caveat about a
  field the question is not about). Of the bullets left, the first that names only an object goes first, then those
  about a used field, then the other object ones, up to 3. The whole text then passes the same number audit.
- When no bullet is kept, the record answer is shown (answer kind `records`, as for an empty facts block), and the
  trace says `-- no field meaning of the knowledge agent fits the answer (merge by code); the record agent's answer is
  shown`. Since the step-2 fixes (below) code may still add its counted line (F8): the answer kind stays `records` and
  the trace says `-- no field-meaning bullet kept (merge by code); code added the line that says what was counted`; a
  Hinglish record answer (F11) gets `-- no field-meaning bullet added (merge by code): the record answer is Hinglish
  and the field meanings are English; the record agent's answer is shown`.

### The step-2 fixes (2026-10-30)

The model evaluation of 2026-10-29 (A/B night run, same-input replay, hand reviews) found bullets the code merge
copied from the lean step although they were wrong or off-topic, thinking-aloud text it showed, and context the merge
call used to add. The fixes (F1-F12) only drop, route or add code lines; code never rewrites a bullet. The catalog
checks (F2, F3, F10a, F10c, F10f) read the record catalog (`pipeline.answer` now passes its objects as a dict, name ->
spec); without a catalog entry they give no verdict and keep the bullet. F4, F10b and F10d read the answer's own
evidence. The review of 2026-10-30 tightened several rules (marked "review" below); the gap check of the same day
added two (F2 d and F10f, marked "gap check").

- **Catalog contradictions (F2).** A bullet is left out when it says a field is a "lookup to `X`" and the field's
  reference targets do not hold X, when it puts a field on an object (`Obj.Field`, "`Field` on `Obj`", "`Obj`'s
  `Field`") that has no such column and documents no such field, or when it calls a picklist a lookup ("`F` is the
  lookup field ...", not "is not a lookup"). Review: a child object placed on its parent ("each `Visit_Guest__c` of
  the `Visit__c`", the child having a lookup to the parent) is no missing field, and a claim after a lookup path whose
  first name is no object (`Visit_Helper__r.Team__c`) is about the path's last field, so no check gives it a verdict.
  Gap check (Q29, F2 d): a bullet that says something is "stored on|in [the] `X` [up to 2 words] row" (or record) of
  a catalog object outside the answer's objects is left out when no lookup links X to any of them, in either
  direction; a child object's row and a parent's record are kept.
- **Legacy claims (F3, F10c).** A bullet that calls a field legacy, deprecated, inactive, obsolete or retired is left
  out when the knowledge marks that field legacy on none of its objects (its raw entry: legacy, deprecated,
  replaced_by, the legacy_attribute role, a concept or meaning that starts with "legacy"). A note about inactive values
  ("keeps inactive legacy values") is no claim. A legacy note about a field the count used is also left out when the
  snapshot fills that field on at least half of the rows: the note would undercut a correct count. A used legacy field
  that is mostly blank keeps its note, as a warning that the count misses records. Review: a predicate claim needs the
  field as its subject ("`F` is marked as legacy", "the field, which is now inactive", "`F` (text) is legacy") and
  only words such as "now", "marked", "as", "still", "a" between the verb and the claim word, so "tells whether an
  employee is active or inactive", "is set to Inactive", "a person is inactive" and "this rule is inactive" claim
  nothing about the field; in "replacing the legacy free-text `F` field" the claim is about `F`, the field after it.
- **Another object's subject (F4).** A bullet whose subject (its first name in backticks) is another object, or a field
  placed on another object, is off-topic, even when it names a used field in passing, unless it also places a used
  field on, or names, an object of the answer. A lookup target ("a lookup to `X`") never counts as the subject; a home
  object named after "linked to" or "copied to" keeps the bullet. On the 10-29 runs this also drops 7 correct,
  on-topic bullets in 6 questions (G17 in both runs, P02, P59, Q15, P43, P53): 8 of its 15 drops there are right (the
  replay's hand check, the 6 sets).
- **The answer's own results and the lookup check (F10).** Left out: a value meaning ("`V` indicates ...") that shares
  no content word with the value's knowledge meaning (a catalog check); "three values" or "the only documented value"
  while a result groups or splits by the field with more values (review: "three active values" is no count, the
  records keep retired values too); "unused" or "not used in practice" while such a result shows 2 or more values;
  "largely unused", "rarely filled" or "mostly blank" (review: a claim about how often the field is filled) only when
  the field is filled on at least half of the records its count results group or split (their blank group; a sum or
  a result filtered on the field gives no verdict); an undercount caveat about a lookup ("only set for ...", "not in
  `X`") when the lookup check shows every other lookup holding the same records; a working-notes tail ("fitting the
  'supported' verb", "(implied by ...)"); gap check (P18, F10f, a catalog check): a field called FLS-hidden
  ("FLS-hidden", "hidden by FLS", "field-level security"; the field as for a legacy claim; no claim after "not", "no"
  or "never") when its raw knowledge entry flags `fls_hidden` on none of its objects (the knowledge flags 67 fields
  of 13 objects).
- **Cut-off and cap (F6, F7).** The unfinished-end test ("... moved on", "... prepares for") applies only to the facts
  block's last line: a token-limit cut can only end the lean reply. At the cap of 3, a used-field bullet whose subject
  is a date field of the counting queries comes first (the order is still shown as the lean step wrote it).
- **The counted line (F8).** When the answer's one counting query is a group_records (count or sum) or a list_records
  without any_of or related conditions, and the record answer leaves out the grouped field (its API name or the path's
  last name), the blank group the ranking left out, a not-blank filter on the grouped field or the list's total, code
  adds one line after the body (and after its code rows), before the bullets: "Counted: `Visit__c` records grouped by
  `Visit_Helper__c`, with `Visit_Date__c` from 2026-05-01 to 2026-05-31 (America/New_York); the blank group (96) is
  left out of the ranking." (a sum reads "Summed `<field>` over ..."; review: the period by its first and last day, "on
  or after <day>" or "up to <day>" for an open one, never the agent's own words such as "since ..." after "in"). Its
  words are `prompts/code_merge_counted.md`; every value comes from the result and its "applied" block, so the number
  audit passes. A line alone keeps the answer kind `records`. Review: a kept bullet that names the grouped field says
  it too, when the record answer or the bullets also name every filter's field (else the line is the only place that
  names the filter's lookup). It is never added after a not-found answer, after a people answer code wrote from
  about_people (review: it states no count) or to a Hinglish answer, and the lookup footer, the status lines and the
  person lead read the answer without it.
- **Hinglish (F11).** When the record answer's prose (its code rows, table rows and backtick spans aside), or that
  prose with its list items (review: a short Hinglish lead over Hinglish list lines), holds at least 3 romanized Hindi
  words, and at least 10%, nothing is added: the facts block is English and code cannot translate it. The words are
  `src/sf_assistant/knowledge/spoken_words.txt` (which the value spots and the resolver read too, so an edit there also
  moves F11 and F5) plus 30 short function words in `agent/merge.py` (11 of them also in that file, the fallback when
  it cannot be read; review: "main", Hinglish for "in" as well as "I").
- **Thinking aloud (F5).** The merge call (not code) gets a record answer that thinks aloud in a prose line ("I have
  the answer.", "It seems ...", "Now I know:"; review: never in a list item or a table row, whose values may read so),
  holds tool talk (a tool name or result key, `checks._TOOL_WORD_RE`, or talk about the prompt; review: not the merge
  input words of `checks.working_notes`, which read "in draft", a picklist value, as merge talk), or is Hinglish with a
  first-person word ("main", "mujhe", "maine"). About 1.5% of the 10-29 answers; each pays one merge call.
- **Shared code (both merge modes).** The code-written people answer drops the internal P/R reference codes, as the
  agent's reply already did (F1, `RecordSession.ask` and `people_answer_from`: also the page's records mode, the
  fallback and a rejected merge); a closest stored name spelled unlike the asked name gives only the not-found lead,
  its name and kind (F9; review: not when it is the asked name written another way, the words in another order, an
  initial, fewer or more words, or for a one-word ask a close spelling of one of its words; a one-letter typo in a
  full name shorter than 7 characters still reads as a look-alike: `docs/people.md`); the record tools' "also stored
  in" note says why none of those records match: "none of them meets the filter <f>" or "those that meet the filter
  <f> fail another condition", from one more COUNT (F12, `tools/record_tools.py`, so a record agent no longer reads it
  as a period exclusion; when that COUNT fails, the note keeps its short form).

Measured with the same-input replay of the 10-29 runs (no model; `.private/eval_2026-10-30_step2fix/replay_fixed`, the
6 sets lookup, golden, people_dev, controls, human_records and human_people), after the review fixes and the gap
check: of the 164 model-run answers with a merge, 5 go to the merge call (3 of them newly, F5) and the code merge
rebuilds 159, of which 44 changed against the 10-29 replay (41 bullets left out, 4 bullets took a freed place, 16
counted lines, 10 answers left with no bullet); of the 163 code-run answers with a merge, 5 go to the merge call (2
newly) and code rebuilds 158, of which 30 answers changed (32 bullets left out, 3 took a place, 8 counted lines, 10
with no bullet), and 5 more changed only in their tool-note inputs (F12). The review left every bullet decision of the
build as it was; it removed 28 counted lines that only repeated what a kept bullet and the record answer already
named, and wrote 3 periods by their days. The gap check's two rules leave out one bullet each, in P18 (F10f, model
run) and Q29 (F2 d, code run), and change nothing else, with `SFA_TABLES` unset and with `code`. The auto grades of
the replayed answers are unchanged in all 6 sets; on the rebuilt code-run answers Q17 improves (F8, its counted line)
and P59 drops (F4 leaves out a correct bullet whose words the grader accepted; its hand verdict is unchanged). The 164
model-run answers still rebuild byte for byte.

Lost context, measured on the same replay (model-run inputs; a number of the 10-29 model merge's text that neither
the record answer nor the question holds): 30 of the 159 rebuilt answers had such numbers, 65 in all. F8 brings back
all of them in 3 of these answers (P44, HR12, HR14) and part of them in a fourth (HR13); 27 answers still miss 60
numbers, 44 of them the days, months or years of a period and 16 counts or totals. Most (35 of the 44) are the
periods of count, trend, compare, aggregate and field-history answers: F8 never writes a line for those (a period
alone does not trigger it, because code cannot tell whether the answer already gives it in words). The rest are
counts or totals (3 of them in G17) and periods F8 does not state (group or list answers where it did not fire or
does not give them, and the Hinglish HR18). The numbers HR16 and HR27 lose were the model merge's own side errors.

**Known residuals** (the gap check of 2026-10-30; ids of the 10-29 runs):
- **Periods.** A period-only counted line (one count_records or trend_records query with a complete period that the
  record answer gives neither by its days nor by its month and year) was measured the same way and not built: it
  would fire on 1 of the 159 answers (HR47, which misses no number) and bring back none of the 60 numbers. Of the 27
  answers, 15 have no single count_records or trend_records query (several queries, another tool or none), 5 already
  name the period's month and year, 5 got an F8 line that does not state the number, 1 has any_of or related
  conditions and 1 is Hinglish (HR18, F11: nothing is added).
- **P30** (code run): the record answer's first-person lead that names the person stays, by design: it says which
  person the answer is about.
- **HR29** (model run): F5 sends it to the merge call, but its 10-29 merge was rejected as working notes, so its
  tool-talk record answer (it ran out of tool calls and lists the group_records and count_records calls it would
  make) would still show; only the added bullet is gone. It is the only F5 case whose visible defect probably stays.
  A fix belongs on the record side (a reply that is only tool talk counts as no record answer) and changes both merge
  modes, so it needs the owner's go.
- **G09** (code run): its last bullet is still read as cut off (F6 keeps the unfinished-end test for the facts
  block's last line).
- **Freed places.** Two weaker bullets move up into places the fixes free: in HR38 (code run) a bullet on an
  interview time field the knowledge says not to use, in HP04 (code run) one on a formula field that exists only in
  dev9.
- **F4's cost.** The 7 correct bullets above (G17 in both runs, P02, P59, Q15, P43, P53).
- **F9, an owner decision.** The review reversed the first design's trade-off: a closest stored name that is the
  asked name written another way (the words in another order, an initial, a missing or extra word, a close spelling
  of a one-word ask) keeps the profile instead of only its name and kind ([people.md](people.md), "A look-alike
  closest name").
- **Only a night evaluation shows** what the merge call writes for the answers F5 sends to it (HR16, P43, HR25, HR08,
  HR29), whether the record agent still misreads the F12 note (HR17, HR20) and HP03's people answer (F1, F9); the
  check list is in `.private/eval_2026-10-30_step2fix/cases_README.md` ("Step-3 night run").

**Apply and rollback (the owner's go only).** The page and the MCP server keep the code they loaded at start-up: the
fixes reach the page after `./run.sh restart-page`, and F12 (the tool note) reaches MCP clients after `./run.sh
restart-mcp` (the MCP server never imports the merge, the people answer, the record session or the pipeline). Never
apply during an evaluation run: the runner starts one subprocess per set, which would mix old and new code.
`agent/merge.py` reads the new `prompts/code_merge_counted.md` when it is imported, and the record session, the lean
step and the page's start-up import it: a tree with the new `src/` but without that file fails on every record
question (the page keeps running without a pipeline, see "Deploy and go-live" under the code-built rows), also after a
crash restart or a host reboot. `diff -rq` lists the file as "Only in ...", not among the changed files, so a stage
built from its "differ" lines misses it. Apply the staged tree (`.private/staging/step2_fixes_2026-10-30`: the changed
and new files at their paths in the sf_assistant folder), from the main folder, in this order:

0. before anything is copied in, save the 21 files the apply replaces (`$OLD` below), as they are in the main folder,
   to `.private/rollback_<stamp>_step2_fixes/` at the same relative paths, with a `README.txt` that lists them and the new
   file and holds the rollback's steps (the snippet below; folders 700, files 600; when it fails, do not go on);
1. the new prompt file only (`prompts/code_merge_counted.md`; the other 92 are byte-identical; old code never reads
   it);
2. `src/` in one command from the staged tree (one `cp -a` or `rsync`);
3. `tests/`, `scripts/`, `docs/`;
4. before any restart, from the main folder:
   `.venv/bin/python -c "from sf_assistant import prompts; prompts.all_prompts(); import sf_assistant.agent.record_session, sf_assistant.pipeline, sf_assistant.server.app"`;
5. `./run.sh restart-page`, then `./run.sh restart-mcp`.

```bash
RB=.private/rollback_$(date +%Y%m%d_%H%M%S)_step2_fixes   # step 0, from the main folder
OLD="src/sf_assistant/agent/merge.py src/sf_assistant/agent/people_answer.py src/sf_assistant/agent/record_session.py
src/sf_assistant/cli.py src/sf_assistant/pipeline.py src/sf_assistant/tools/record_tools.py
tests/parity/harness.py tests/parity/test_p0_code.py tests/test_agent_eval_fixes.py tests/test_agent_record_session.py
tests/test_code_merge.py tests/test_code_tables.py tests/test_pipeline_cli.py tests/test_prompt_files.py
tests/test_tools_records.py scripts/gen_prompts_doc.py docs/architecture.md docs/people.md docs/prompts.md
docs/runbook.md docs/spec/RECORDS_SPEC.md"
(umask 077 && mkdir "$RB" && cp --parents $OLD "$RB" && cat > "$RB/README.txt" <<EOF
The step-2 fixes of 2026-10-30 (the merge by code's fixes F1-F12, their review and the gap check), sf_assistant
only: copies of these files as they were before the apply, at the same relative paths:
$OLD
New file (delete it to roll back): prompts/code_merge_counted.md.
Rollback, only on the owner's go, from the sf_assistant folder (the docs/runbook.md here is the one of before, without
these steps): src first (agent/merge.py reads the prompt file at import), then tests, scripts and docs, then delete the
prompt file; then the import check and the restarts.
  cp -r $RB/src/. src/
  cp -r $RB/tests/. tests/ && cp -r $RB/scripts/. scripts/ && cp -r $RB/docs/. docs/
  rm prompts/code_merge_counted.md
  .venv/bin/python -c "from sf_assistant import prompts; prompts.all_prompts(); import sf_assistant.agent.record_session, sf_assistant.pipeline, sf_assistant.server.app"
  web/run.sh restart-page && web/run.sh restart-mcp
EOF
)
```

The merge call (`combine_system`, `agent/merge.py` `combine`, unchanged) still runs when the full knowledge agent
answered (a design question, `--full-knowledge`, an unclear route, a value overrule, the lean step falling back to the
full agent), whenever there is a FIELD CHECK line (it can change which count leads) and when the record answer still
plans aloud ("Let me compile the comprehensive answer.", `merge.plans_aloud`: the merge call rewrote such a lead away;
since the step-2 fixes also thinking aloud, tool talk and Hinglish first-person planning).
Everything after the merge is the same for both: the answer choice, the contact rule, the lookup footer, the status
lines and the "Read as" line. The lookup footer, the status lines and the person lead of the spelling help read the
answer without the lines the merge by code added (`merge.code_merge_lines`), so a bullet that names the lookup or a
status value never takes away the code line that says what was counted.

On the two saved standard evaluation runs (238 lean answers, evidence rebuilt on the local snapshot, no model) the
review's rules kept 371 bullets instead of 607: every bullet a reviewer had marked as contradicting the record answer
(28) or as speculation or meta text (23) is left out, 20 of the 24 marked irrelevant, and 63 of the 88 marked fine
are kept (most of the others name a person of the question). 175 answers get bullets, 63 show the record answer as it
is (54 of them people answers).
Before the change (page log, 41 merges, all lean): median 9.8 s per merge, 562 s in all (21% of those questions'
time), a merged text a median 0.984 similar to the record answer, and 4 merged texts thrown away. A code merge takes
milliseconds; the timeline keeps its `merge` stage (about 0 s).

The switch is read at each answer (`config.merge_settings`, listed in `ENV_DOC`):

| `SFA_MERGE` | A lean answer without a FIELD CHECK line |
|---|---|
| `code`, unset or empty (the default) | merged by code, no model call |
| `model` | the merge call, as before 2026-10-29 |
| anything else | merged by code; its JSONL says `merge_switch: "SFA_MERGE must be code or model"` (nothing stops) |

```bash
./ask.sh "how many client interviews ended with an offer"                  # merged by code
SFA_MERGE=model ./ask.sh "how many client interviews ended with an offer"  # the merge call, as before
SFA_MERGE=model .venv/bin/python -m sf_assistant.evaluation run ...         # an A/B evaluation run (owner's run only)
```

The JSONL row (the page log's `detail.jsonl`, `--file` and evaluation sidecars) gets `merge_by` (`code` or `model`)
whenever a merge ran, and `merge_switch` only for an invalid value; `merge_seconds`, `merge_error` and
`rejected_numbers` are as before (a code merge that kept no bullet has no `merge_error`). The evaluation runner's
`ask --file` subprocess inherits the shell's environment, so `SFA_MERGE=model` selects the merge call for a whole run.

**Rollback.** In the terminal and for evaluation runs: `SFA_MERGE=model`. The page reads its own environment, and
`web/compose.yaml` does not pass `SFA_MERGE` into the container, so after its next restart the page merges by code
(the MCP server runs no merge; its `combine_answers` prompt is unchanged). To bring the merge call back on the page,
copy the files of `.private/rollback_20261029_code_merge/` back (its `README.txt` lists them) and `./run.sh
restart-page`, or add `SFA_MERGE: "${SFA_MERGE:-}"` to the page's `environment` in `web/compose.yaml` and run
`SFA_MERGE=model ./run.sh up` (recreates the page and signs everyone out). Either only on the owner's go.

## How record rows are written (SFA_TABLES)

The record agent types every row of a table or list it shows, at 23-36 tokens per second on the shared server: up
to 1,500 tokens and 30-65 s for a long answer (`.private/latency_2026-10-28.md`). Latency plan step 3 (O1, the owner's
go of 2026-10-29) lets code write those rows instead (`agent/tables.py`). It is behind the switch `SFA_TABLES`, and
the default stays `model` (the agent types its rows, as before) until the owner's go-live.

With `SFA_TABLES=code`:

- The record agent's system prompt ends with `prompts/records_tables_note.md` (appended in `RecordSession.ask`;
  `render_records_system`, which the MCP template `answer_from_records` also uses, is unchanged). The agent writes
  1-3 sentences with the numbers the question asks for, then for each list, group, trend or compare result or count
  breakdown with 2 or more rows to show a line `{{table q3}}`, where `q3` is that result's `query_id`, then one bullet
  on what was counted (field and lookup API names in backticks), any caveat and the Source line (the review of
  2026-10-30 kept the bullet: a lookup answer that names its field only there still grades as before). It types no rows
  in any form. This is the note's version 2 (step-3 fix T2, after the night A/B of 2026-10-30: rows shown twice, list
  first answers, false totals); no table line for `about_people`, `find_references` or `get_field_history`. No model has
  read version 2 yet: only a night A/B, on the owner's go, can measure it.
- Code replaces each table line with that result's rows, right after the reply's cleaners, so every later check reads
  what the user sees (`tables.expand`); each table line is first put on a line of its own (`tables.own_lines`), so a
  cleaner that drops a planning sentence ("Let me list them: {{table q1}}") keeps it. The rows come only from what the
  model received (`QueryEvidence.model_result`, after the result cap), never from the SQL statements: the same rows,
  order, labels and values. Each draft is checked the same way (the model's history keeps its own draft with the table
  lines), except that the first check sends no working-notes or number message that only a code-built cell sets off
  (a stored note "I will call back", "as per instructions" or "in draft", a line break before a number): the model
  never typed it and cannot change it, and the message would cost a round.
- The answer form (owner's choice D3, the forms today's answers and graders read): `list_records` rows are a markdown
  table whose headers are the catalog labels of the columns (" (name)" and a lookup's " ID" dropped, a repeated header
  prefixed with its first hop, or, for a field of the object itself, its API name in words: `Scheduled_Start__c` reads
  "Scheduled Start"; never an API name); `group_records` groups, `trend_records` periods, `compare_records` segments
  and a `count_records` breakdown are `- label: value` lines, a split inline on its row
  (`- Completed: 5 (Full Time: 4, Contract: 1)`; a split value a row lacks is left out, never written as 0; a
  breakdown of several fields gets a label line per field; a period the result marks partial, its first or last,
  ends with `(partial)`). Values stand as the result gives them (numbers in their JSON form, dates, buckets,
  `(blank)`, `(unresolved id)`, `Name (#2)`, `[hidden]`); in a table cell `|` reads `¦` and a line break one space, a
  null is an empty cell (`(no value)` on a line).
- Never written by code: `about_people` and `find_references` (people answers carry reference codes and can be longer
  than the model saw), `aggregate_records`, `percentage_of_records`, `search_records`, `get_field_values`,
  `get_field_history`, `get_sync_status`, the orientation tools, a failed call, a count without a breakdown, a result
  that did not reach the model whole and an unknown id. Such a line is removed and counted.
- Under 2 or more groups or periods a `- Total: n` line only when the result holds the total of exactly the rows shown
  and they add up to it (none under one row, whose line it would only repeat; never with top N, the 50-group cap, a
  trimmed result or a multi-select field; code never adds numbers up). Under a list cut by its limit, the 50-group
  cap or the result cap, a "Showing k of n records." line from the result's own numbers ("Not all records are shown."
  when k is no number the result holds), unless the model's text already gives both numbers.
- Code cuts nothing the tool returned (the tools already cap: a list at most 50 rows, 50 groups, a trend at most 120
  periods, 4 segments, a breakdown 8 values and `(other)`). `TABLE_MAX_ROWS = 120` is only a safety net (one
  number-free `…` row ends the block).
- Placement: a block goes where its table line was; a table line before the first prose line moves after the first
  prose paragraph (the lead stays first), one after the Source line moves before it when that line ends the text (a
  Source line written first stays where it is, below it the lead and then the rows), one inside a sentence leaves the
  sentence and its rows follow that line; a list marker or a joining word left with table lines (`1.`, `and`, `,`)
  goes with them. One blank line before and after each block. A `- ` block right after another block starts with a
  number-free label line of its result (`By Status:`, `By month:`, `Compared:`; the words are in
  `prompts/records_table_words.md`): two lists that only a blank line separates read as one on the page (Markdown,
  like `web/static/render.js`, joins them). A line of the model's own between them ("By status:") needs no label.
  A `- ` block that would open the answer, written before any other text, starts with its label line too, only when
  `nudges._lead` skips that label (no digit, `nudges._LABEL_LINE_RE`), so the lead checks read the same; the merge side
  never adds one. List tables get no label. The model's own bullets right after a `- ` block still read as part of its
  list, as with typed rows.
- Fallbacks: a line code cannot fill is removed and counted, and the first draft check names it
  (`prompts/records_tables_marker_message.md`) only when it sends another message anyway (joined, no extra round) or
  when nothing else of the draft is left; a dropped line costs no model round otherwise. A near miss of the table line
  (`{table q3}`, `{{table q3}`, `{{table_q3}}`, `{{tbl q3}}`, `{{query q3}}`, `{{q_3}}`) is such a line too (removed,
  named, counted as `malformed`). When the model typed every row too, each on a line of its own, its own copy stays
  and the line goes (`typed`). A row is typed by a list or table line of the model's own text that writes its key and
  no other row's key, and one of its values; a row with a split needs every split value, each in a place of its own,
  and the row value or a split that adds up to it (a split of one value equal to the row value also needs its label or
  the number twice: one number alone may be only the total). A key counts as a whole word anywhere; a `YYYY-MM` key also as
  `June 2031` or `Jun 2031` and `(blank)` also as `blank`, `no value` or `not set`, these forms only first on the line,
  never a bare month. A non-negative number counts in the forms `checks.numbers_in` reads (`$1,234.00`, `1,234`, `1234.0`),
  the line's dates, times, year-months and record codes aside (`2031-05`, `05/2031`, `GO-00005`); anything else, a
  negative number too, as a whole word. Any other written form (a week, a quarter, a numbered line, a number word) types
  nothing: the block stays and the rows show twice, nothing is lost. A list line that names no row's key types the
  one row whose other cells, at least 3, are all on it; when that drops the table, its first column is no longer shown.
  Of a dropped block, the Total line stays just before the Source line when it holds split totals that no line of the
  model's own text gives (stored under its table line, with its label only right after another block). When a check of
  `RecordSession.ask` then cuts a line of that copy (the number audit above all: a line can hold a number no result
  gives), code's whole block comes back (`tables.put_back`, in place of a kept Total line, or just before the Source line;
  counted as `put_back`), so the row is not lost; a value or two may then show twice. Rows typed only
  in part (a bullet that names the top groups types none) are filled all the same and counted (`partly_typed`), so no
  row is lost. Nothing in `agent/tables.py` raises: an internal error gives the
  text without its table lines, and as a last resort `pipeline.answer` removes any table line left in the final
  answer.
- Checks: the blocks' own text is a source of the number audits (a stored line break reads as `n15` in the record
  facts and as `15` in a cell), and the contact rule runs on the final answer and on the stored blocks.
- Merges: the merge by code keeps the rows verbatim (after a list, its label line first), and `merge.plans_aloud`
  reads the record answer with its rows as table lines (a cell such as "I will call back" forces no merge call). The
  merge call gets the record answer with its rows as table lines and `prompts/combine_tables_note.md` appended to its
  system prompt; every merge check still reads the record answer with its rows; code then puts the rows back
  (`tables.restore`: a lost line's rows go before the Source line, a repeated line is filled once, a line in a code
  fence comes out of it, rows the merge typed itself, every row on a line of its own, stay, by the same typed rule as
  `tables.expand` and with the same Total line left, rows it typed only in part get the block all the same, and a line of
  its own right above a table line that repeats the block's label line goes, so the label shows once) or, when
  that fails, shows the record answer (`merge_error` "the record answer's rows could not be put back").

The switch is read at each answer (`config.tables_settings`, listed in `ENV_DOC`; `RecordSession.ask` reads it once
per record question, `pipeline.answer` once per answer):

| `SFA_TABLES` | Who writes the rows |
|---|---|
| unset or empty | `config.TABLES_DEFAULT` (`model` until the go-live) |
| `model` | the record agent types them, as before 2026-10-29 |
| `code` | the record agent writes a table line per result, code writes the rows |
| anything else | `TABLES_DEFAULT`; the JSONL says `tables_switch: "SFA_TABLES must be model or code"` (nothing stops) |

The JSONL row gets, in code mode only, `tables_by: "code"` and `code_tables`: the counts of the table lines
(`markers`, `built`: blocks with rows, so a kept Total line alone does not count, `rows`, `dropped` by reason:
`unknown`, `failed`, `no_rows`, `empty`, `unseen`, `repeated`, `typed`, `malformed`; `no_rows_by`, only when there is
one: the `no_rows` drops by tool name; `partly_typed`: blocks filled although the model typed some of their rows;
`items`: query id, tool, rows and form of each block; `kept`: blocks the final answer holds whole, after the merge, a
kept Total line included; `chars`: the summed length of the blocks the record answer holds, what code wrote into it;
`cut`: the table lines of the reply that `strip_reasoning` removed before `tables.expand` saw them; `left`: "{{" or
"{table" the final answer still holds, a near miss no pattern reads or a stored value with braces; `nudged`: the first
draft was asked to fix its table lines; `merge`, when the merge call got table lines: `merged` 1 or 0, 0 when its
text was not used (`merge_error` says why), and for a used text how the rows came back: `lines`, `lost`, `retyped`,
`repeated`, `partly_retyped`, or `error` when they could not be put back; `put_back`, only when there is one: blocks
dropped as typed that came back because a check cut a line of the model's copy; `built`, `rows` and `items` do not count
them, and the transcript's `code rows:` line gives them as `put back: N`), all numbers, fixed words, query ids and tool names. In every mode it also gets `record_nudge_kinds`: the prompt id of each draft-check message, one per message
and in order (`nudges.nudge_kinds`, read from the message's text; "other" for a text no prompt fills; `[]` when there
is none), beside `record_nudges`, which keeps one cut text per check. The require-tools and force-answer messages are
not in it (the timeline's `tools_first` and `step_cap` show them). `--file` transcripts show one `code rows:` line under
the record agent's tool lines. In model mode the JSONL gains only `record_nudge_kinds`; the transcripts are as before.

The night evaluation (owner's go only) runs with the switch exported and the merge switch unset (merge by code), on
the same sets and level as the `SFA_MERGE=code` arm of 2026-10-29 it is compared with: the package's sets and the
human-like sets (`.private/questions/human_like/`, whose records set `human_records` holds the page-style list and
table questions), plus any table-heavy questions the owner approves:

```bash
export SFA_TABLES=code
unset SFA_MERGE
.venv/bin/python -m sf_assistant.evaluation run --set all --level standard
.venv/bin/python -m sf_assistant.evaluation --sets .private/questions/human_like/sets.yaml run --set all --level standard
```

Judge O1 first by the record agent's last-call completion tokens (`evaluation timeline`) and `code_tables` (markers,
built, kept, chars, cut, left, dropped by reason, no_rows_by, partly_typed, put_back, nudged), and by seconds (`record_seconds`)
only on the page-like human-like set: the package sets' saved answers hold no table, and their value lists save about a
second each, within the run-to-run spread. Read `record_nudges`, `record_nudge_kinds` and `merge_error` too, and the
grader verdict changes by their reasons. When the step-2 fixes are in the tree, also read by hand the cases only a model
run decides (the merge
call's text for the answers F5 sends to it, the F12 note, HP03, F4's cost: the check list in
`.private/eval_2026-10-30_step2fix/cases_README.md`, "Step-3 night run").

**Deploy and go-live.** `web/compose.yaml` passes no `SFA_TABLES`, so the page and the MCP server run
`TABLES_DEFAULT`. The page imports every module of `sf_assistant.agent` at start-up (`web/pipeline.preload_package`),
even in model mode, so `agent/tables.py` makes its 4 prompt files start-up needs, and it parses every prompt file
(`prompts.all_prompts()`); every record session (the cli, the evaluation runner) imports `agent/tables.py` too. A page
whose start-up failed keeps running without a pipeline (`web/server.py` catches the error, so `restart: unless-stopped`
does not help): a restart (a host reboot too) on a half-copied tree leaves the page down, or answering every record
question with an error, until `./run.sh restart-page` runs on a whole tree. Apply the staged tree
(`.private/staging/step3_code_tables_2026-10-29`: the changed and new files, at their paths in the sf_assistant
folder) only after the evening evaluation has finished, in this order:

0. before anything is copied in, save the 17 files the apply replaces (every staged file the main folder already
   has), as they are in the main folder, to `.private/rollback_<stamp>_code_tables/` at the same relative paths, with
   a `README.txt` that lists them and the 6 new files and holds the rollback's steps (the `docs/runbook.md` copied
   back is the one of before, without them): the snippet below, from the main folder (folders 700, files 600; when
   it fails, do not go on);
1. the 4 new prompt files only (`records_tables_note.md`, `combine_tables_note.md`, `records_tables_marker_message.md`,
   `records_table_words.md`; the other 88 are byte-identical);
2. `src/` in one command from the staged tree (one `cp -a` or `rsync`). File by file, only in this order:
   `agent/tables.py`, `agent/results.py`, `config.py`, `agent/merge.py` and `agent/render.py`, then
   `agent/record_session.py` and `pipeline.py` last (a new `record_session.py` with the old `results.py` starts but
   fails every record question with a `TypeError`; with the old `config.py` the page does not start);
3. `tests/`, `scripts/`, `docs/`;
4. before any restart, from the main folder:
   `.venv/bin/python -c "from sf_assistant import prompts; prompts.all_prompts(); import sf_assistant.agent.tables, sf_assistant.agent.record_session, sf_assistant.pipeline, sf_assistant.server.app"`.

```bash
RB=.private/rollback_$(date +%Y%m%d_%H%M%S)_code_tables   # step 0, from the main folder
OLD="src/sf_assistant/config.py src/sf_assistant/pipeline.py src/sf_assistant/agent/record_session.py
src/sf_assistant/agent/merge.py src/sf_assistant/agent/render.py src/sf_assistant/agent/results.py
tests/parity/harness.py tests/parity/test_harness_selfcheck.py tests/conftest.py tests/test_layering.py
tests/test_prompt_files.py tests/test_settings.py tests/test_mcp_server.py scripts/gen_prompts_doc.py
docs/runbook.md docs/architecture.md docs/prompts.md"
(umask 077 && mkdir "$RB" && cp --parents $OLD "$RB" && cat > "$RB/README.txt" <<EOF
Code-built record rows (latency plan step 3, O1; SFA_TABLES), sf_assistant only: copies of these files as they were
before the apply, at the same relative paths:
$OLD
New files (delete them to roll back): src/sf_assistant/agent/tables.py, tests/test_code_tables.py and the prompt
files prompts/records_tables_note.md, prompts/combine_tables_note.md, prompts/records_tables_marker_message.md and
prompts/records_table_words.md.
Rollback, only on the owner's go, from the sf_assistant folder (the docs/runbook.md here is the one of before,
without these steps): src first, then tests, scripts and docs, then the new files, agent/tables.py before the prompt
files (it reads them at import, and the page imports every agent module at start-up). The prompt files must go too,
or the package's tests fail; the page starts either way. Then the import check and the page restart.
  cp -r $RB/src/. src/
  cp -r $RB/tests/. tests/ && cp -r $RB/scripts/. scripts/ && cp -r $RB/docs/. docs/
  rm src/sf_assistant/agent/tables.py tests/test_code_tables.py
  rm prompts/records_tables_note.md prompts/combine_tables_note.md prompts/records_tables_marker_message.md prompts/records_table_words.md
  .venv/bin/python -c "from sf_assistant import prompts; prompts.all_prompts(); import sf_assistant.agent.record_session, sf_assistant.pipeline, sf_assistant.server.app"
  web/run.sh restart-page
EOF
)
```

Before a deploy, check that `config.TABLES_DEFAULT` is still `"model"` unless the owner gave the go-live
(`grep -n 'TABLES_DEFAULT =' src/sf_assistant/config.py`). The go-live is one line, `TABLES_DEFAULT = "code"` in
`src/sf_assistant/config.py`, then `./run.sh restart-page`, both only on the owner's go. **Rollback** of the switch:
set `TABLES_DEFAULT` back to `"model"` and restart the page; in a terminal or for an evaluation run `SFA_TABLES=model`
is enough. **Rollback of the files**, only on the owner's go, from the folder `RB` of step 0 (its `README.txt` holds
the same steps with the folder's name filled in: the `docs/runbook.md` copied back no longer has them), in this
order: `src/` in one command (`cp -r "$RB/src/." src/`, which keeps the modes of the files it overwrites; file by
file, `agent/record_session.py` and `pipeline.py` first, then `agent/merge.py`, `agent/render.py`, `agent/results.py`
and `config.py`), then `tests/`, `scripts/` and `docs/` the same way, then delete the 6 new files: `agent/tables.py`
and `tests/test_code_tables.py`, then the 4 prompt files (`agent/tables.py` before them: it reads them at import, and
the page imports every agent module at start-up). The 4 prompt files must go too: with them the package's tests fail
(the count of 88 prompt files, the checks that every file is bound and loaded, `tests/test_prompts_doc.py`), although
the page itself starts either way (the old code never reads them). Run the import check of step 4 (without
`sf_assistant.agent.tables`) before a restart. The MCP server runs no record agent and is unchanged either way: its
tools, envelopes and prompt texts (`answer_from_records`, `combine_answers`) are the same with either value.

**Apply and rollback of the step-3 fixes T1, T2, T3, T5 (the owner's go only).** `TABLES_DEFAULT` stays `model`, so nothing
goes live. The page and the MCP server keep what they loaded until a restart. A restart or a host reboot after the apply
loads the new code (the page bind-mounts the main folder), and model-mode rows then gain only the key
`record_nudge_kinds`, answers unchanged; the MCP server imports none of these modules. Never apply during an evaluation
run. From the main folder, in this order:

0. before anything is copied in, save the 13 files the apply replaces (`src/sf_assistant/agent/results.py`,
   `src/sf_assistant/agent/nudges.py`, `src/sf_assistant/agent/tables.py`, `src/sf_assistant/agent/record_session.py`,
   `src/sf_assistant/pipeline.py`, `src/sf_assistant/agent/render.py`, `prompts/records_tables_note.md`, `tests/test_code_tables.py`,
   `tests/parity/harness.py`, `tests/parity/test_harness_selfcheck.py`, `docs/prompts.md`, `docs/runbook.md`,
   `docs/architecture.md`) to `.private/rollback_<stamp>_step3_fixes/` at the same relative paths (folders 700, files
   600; when it fails, do not go on);
1. `src/` in one command from the staged tree, or file by file only in the order `agent/results.py`, `agent/nudges.py`,
   `agent/tables.py`, `agent/record_session.py`, `pipeline.py`, `agent/render.py` (a new `record_session.py` fails at import with an old
   `nudges.py`, and fails every record question with a `TypeError` with an old `results.py`);
2. `prompts/records_tables_note.md`, then `tests/` (the new file `tests/test_code_tables_forms.py` too) and `docs/`;
3. before any restart, the import check of step 4 above.

Roll back the same way in the reverse order, from that folder, and delete `tests/test_code_tables_forms.py`, then run the
import check. The rollback text above belongs to the first apply of the code rows: it would also undo the step-2 fixes
and delete `agent/tables.py`, so it does not apply here.

## Charts (SFA_CHARTS)

When a question asks for a chart, graph or plot (English, Hinglish, common typos), the answer shows the picture and,
under it, the table of numbers it was drawn from. When a chart makes no sense (one number, no rows, too many groups,
hidden names), one plain line says why, and the numbers stay in the answer. Code draws it (`agent/charts.py`): the
model fetches the data as it does today, and code reads those results after it finishes, so the model never types a
chart number or writes chart code. Nothing new is installed, and the page's security headers do not change. It is
behind the switch `SFA_CHARTS`, and the default is `on` since the owner's go-live of 2026-11-04. With it off nothing
below runs: answers, model requests, MCP listings and envelopes are as before.

With `SFA_CHARTS=on`, on the answer path:

- **The ask.** `charts.chart_ask` reads the question once per answer, with code only. A question that does not ask for
  a chart gets no note, no key and no line, and costs about 25 microseconds.
- **A bare ask.** A question that names nothing to chart gets one line and no model call: `now plot that`, `chart it`,
  `iska graph banao`, and the same with only a split or a period beside a word that points back (`plot that by month`,
  `make it a pie chart by recruiter`, `plot this for last year`, `iska graph month wise banao`). Its `answer_kind` is
  `none`.
- **The route.** A `NO_RECORDS` route on a chart question is overruled in `RecordSession.triage`, so the question
  reaches the record agent. It keeps the full knowledge agent, as a value overrule does, so a design question keeps its
  design answer. The JSONL row's `triage` says `overruled_by` `chart`, and the first line of the record trace reads
  `-- route: the route call said NO_RECORDS; overruled by a question that asks for a chart`. When the merge of such a
  question is rejected or fails, the shown text holds the knowledge answer and then the record answer, and the answer's
  kind still reads `records`.
- **The note.** The record agent's system prompt ends with `prompts/records_chart_note.md` (appended in
  `RecordSession.ask`; `render_records_system`, which the MCP template `answer_from_records` also uses, is unchanged).
  It is 471 characters, about 131 tokens a round. It steers the agent to `group_records` or `trend_records`, names
  `last 12 months` when the question names no period, and tells it never to type a chart or to say whether one can be
  drawn. The agent keeps its 14 tools.
- **What the model says about a chart.** `charts.drop_talk` removes it, on chart questions only: a fenced block of
  plotting code, a chart mark or image, block characters, and a sentence that names a chart and holds no digit. Table
  rows, the rows code wrote, the Source line and the end of a list item with no closing stop (a row, `- Chart Analyst
  (Open)`) stay. It runs on the record agent's reply before the merge decision, and on the final text only when that
  text is the record agent's (`records`, or `merged` with the lean step). A knowledge answer and a full-mode merge keep
  their sentences, so "The recruiting dashboard has a bar chart." stays, and only lose a chart mark. What code wrote (a
  not-found or people answer) keeps its words, because a stored name can hold a chart word. When only a Source line or a
  label would be left, the sentences come back, and marks, images, block characters and plotting code never do: a reply
  of nothing else counts as no answer. Only code talks about whether a chart exists.
- **The drawing.** `charts.make` runs after the contact rule, just before `body = final`, and never raises. It reads
  the record results (`QueryEvidence.model_result`, what the model received), picks the last chartable one (a
  successful `group_records`, `trend_records`, `compare_records`, or a `count_records` with a breakdown), chooses the
  type and the title, draws one SVG in pure Python and checks it. A fault gives the `fault` line and leaves the answer
  whole. No timeline stage is added.
- **The reason line.** An answer gets at most one, before the Source line, in the final text and not in `answer_body`.
  The words are in `prompts/records_chart_words.md`. In order: `records_off`, `failed`, `answer_failed` (the calls
  worked, the record step gave no answer: pipeline.records_failed), `no_data`, then the reason of
  the last data result: `people`, `one_number`, `one_row`, `list`, `one_share`, `field_values`, `no_field_data` (a
  field the query filters on is blank on every record, so the data does not record this and no 0 is drawn), `all_zero`,
  `no_positive` (zeros beside nulls), `no_rows`, `no_rows_min` (no named group reaches the minimum asked for; a blank
  or unresolved group can pass it, and `top` still leaves that group out), `only_blank` (records match, and a `top`
  ranking left them all out: blank in the grouped field, or pointing to a record not in this copy), `no_rows_top` (a
  `top` ranking under a measure other than count has no named group: no record matches, or they all point to a record
  not in this copy, and the result cannot tell which), `no_values`, `too_many`, `hidden`, `same_labels`, `negative`,
  `dates`, `too_large` (also when a cut left no row), `fault`.
- **A rate per period** (2026-11-16). `trend_records` with `part_filters` returns, per bucket, `part`, `base` and
  `percent` (`value` = percent; a bucket with no base record has none, never 0%) and the window's `part_total`,
  `base_total` and `percent_overall`, all in one call. Its chart is a line (or the bar asked for) of `Percent`, with the
  part and base counts in the rows, the whole period in the subtitle and a note on the periods with no base record.
  A question that asks for a rate, share or percentage (not a bill or pay rate) never gets counts over time drawn in
  place of the rate (`trend_records` or `compare_records` with a count measure: `rate_counts`); a share by group (a
  pie) and an average, sum, min or max of a field (a rate field among them) are drawn, and a single percentage keeps
  its `one_share` line.

What a chart holds:

- **Type.** Code decides; the model has no say. A type the question names wins when the data can carry it, otherwise
  the chart's note says why it is another type. With none named: a bar chart, horizontal above 8 rows or with a label
  over 16 characters; a line for a trend; stacked bars when a split adds up. Code never picks a pie by itself, and a pie
  or donut needs rows that are parts of one whole.
- **Caps.** 30 bars, 120 line points, 8 slices, 8 series. Over 30 bars or 120 points: the `too_many` line, and the rows
  stay. More than 8 slices: another type with a note. More than 8 series: the totals with a note, and the series stay in
  the rows.
- **Numbers.** Only from the result: no percents, no donut centre number, no "Other" and no total code made up. Bars and
  lines start at 0. Null is never drawn as 0. The one 0 code draws is on a line per series under count, where a series
  a period lacks means no records; the rows table leaves that cell empty.
- **Words.** Title, subtitle and notes hold catalog labels, dates and the result's own numbers; a compare's partial note
  also names its cleaned labels. A filter is named by its field's label, never by its value or raw text (`Filtered on:
  Round`; a person's filter by its lookup, `Filtered on: Interviewer`), and a text that starts with no known field reads
  `another field`. A cut is said from the result's own numbers (`Showing the top 5 of 40 groups.`).
- **Never drawn.** `get_field_values` and `get_field_history`, because they ignore filters and periods, and a
  true-looking chart could be wrong. `about_people` and `find_references`, and no rows are shown for them.

Where it shows:

| Surface | What it gets |
|---|---|
| Page | The job reply's `charts`, for friend and admin alike. Under the answer text, an `<img>` holding the SVG as base64 and the rows table (hidden when the answer already holds those rows, which only `SFA_TABLES=code` can do). No hover: every value is in the table (or in the answer text when `rows_in_answer` is true), and on the marks where it fits. A wide chart scrolls in its own box |
| Terminal and `--file` transcript | Heading, subtitle, notes and rows as text (`print_outcome`). No file: containers are read-only here, and `*.svg` is not git-ignored |
| Sidecar | The key `charts`, with no picture |
| MCP | Only `chart: true` on the three tools above. The server runs no answer path, so there is no note and no overrule; a result that cannot be drawn gives a null `chart` with its `reason` |

**The switch.** `config.charts_settings` reads `off` or `on`; unset or empty is `config.CHARTS_DEFAULT`, `on`. Any
other value is off, stops nothing, and the answer's JSONL says `charts_switch` (the MCP server writes no such line). `pipeline.answer` reads it once per answer,
`RecordSession.triage` and `.ask` per call, and the MCP server once at start, because its listing is built once.
`web/compose.yaml` does not pass it, so the page and the MCP server run `CHARTS_DEFAULT`; in a terminal or for an
evaluation `SFA_CHARTS=off` turns it off for that run. The page imports `agent/charts.py` at start-up even with the switch off
(`preload_package`), and a failure there means the page cannot start after a reboot, so that module and its two prompt
files go in together.

**Go-live: done 2026-11-04, on the owner's word.** One line, `CHARTS_DEFAULT = "on"` in `src/sf_assistant/config.py`,
then `./run.sh restart-page` and `./run.sh restart-mcp`, with no compose edit and no rebuild. The restart signs everyone
out, as every restart does ([Rules](#rules)). `tests/parity/harness.py` `no_model` pins `SFA_CHARTS=off`, as it pins
`SFA_TABLES`. `/static` files carry an ETag and no `Cache-Control`, so a browser that still holds the old `app.js` can
show no chart until it reloads the file (Ctrl+F5). **Rollback** of the switch: set `CHARTS_DEFAULT` back to `off` and
its pin in `tests/test_settings.py`, and restart both.

**Limits, stated plainly:**

- **Detection is about 87%.** That is one blind list: 45 of 52 phrases. A miss gives a normal answer with no chart, and
  the person asks again using the word "chart". A question that only looks like an ask can fire too
  (`Chart Industries`).
- **A right chart is not promised.** Code draws what the model fetched. If the model fetches only one number, the person
  gets the reason line, not a chart: 1 of the 23 asks for counts (C01-C23) in the model test of 2026-11-04. If the model's query is wrong, the chart
  is true but about the wrong thing, as the text would be, and nothing warns: the heading says what was counted and
  which fields filtered it.
- **"now plot that" is refused.** Earlier results are not kept, so it gets a one-line reply at once asking to name the
  data, which saves the about 31 s of a full answer. So does `plot that by month`: in the model test of 2026-11-04 it
  took a full answer and the agent charted an object nobody asked for. A follow-up with any other word (`plot that for
  west region`) or with no word that points back (`chart by month`) cannot be told from a new question, so it takes a
  full answer and the agent can guess.
- **An answer gets one chart, of its last chartable result.** A `count_records` breakdown gives one per field, 2 at
  most. When several results could be drawn, the chart's note says so (`several`), and the other numbers stay in the
  text.
- **A design question with a chart word costs time.** `which dashboards have a bar chart` keeps its full design answer
  and costs about 34 s of record rounds alongside it (about 3.6 rounds at 9.4 s), plus a merge call of 7-9 s if the
  record side answers. A true chart ask that routes `RECORDS` stays lean and pays none of this.
- **`drop_talk` and the detector have seen few real chart questions.** `drop_talk` ran on 24 authored texts and in the
  model test of 2026-11-04, where it removed something on 10 answers. The detector ran on authored lists and on about 860 real questions (a list that grows with the page log), none of which asks for a
  chart, so those test silence only. Gaps accepted on chart questions: "The chart shows 120 interviews." stays, because
  of the digit; "The record shows how the graph API was set up." drops from a record answer; a list row that ends in a
  stop and holds a chart word and no digit (`- Chart Analyst, Open.`) reads as a sentence and drops; a reply of only
  "Let me plot this.", with or without its Source line, comes back unchanged, so it forces a merge call.
- **One real model test so far.** 2026-11-04, 82 questions on the local snapshot: 30 charts, all 341 chart cells appear
  in the answer text, 0 `charts_error`, rounds mean 3.06 against 2.97 for the same questions without chart words, input
  tokens +11%, seconds mean 23.08 against 25.12. The final fixes of that day (the bare ask, the middle label cut, the
  plural titles) are code only and did not rerun on the model.
- **Only Chrome was tested.** Firefox, Safari and a real phone were not.
- **Not every mark carries its value.** A column chart writes a value that is wider than its slot only on the highest,
  the first or the last column, and only where it meets no other value and no taller column, so a dense chart can show
  one value on 20 columns. A stack writes its total, not its parts. A line writes its first and last values and, where
  it fits, its highest. A line per series writes only its last value, and leaves that out when it cannot sit within
  14 px of its point. The table, or the answer text when it holds the rows, has them all.
- **Labels that differ only where both cuts fall can lose the chart.** The axis cuts a row label at 28 characters, a
  legend at 22, at the end, or in the middle (keeping the end) when only that tells the labels apart. Labels that read
  the same either way give the `same_labels` line and the rows, and no chart. That is the rule, because bars that cannot
  be told apart are worse than none.
- **Label widths are estimated.** They come from a table of character widths and are never measured in a browser, so a
  label can sit closer to its neighbour than the shots show.
- **"no show chart" gets no chart.** The refusal rule reads `no show chart` and `no show chart by recruiter` as "no,
  show a chart" turned down, so the person gets a normal answer, no chart and no line. `no-show chart by recruiter` and
  `no show interviews chart` do fire. The owner decides (D7).
- **A question over 1000 characters is no ask,** counted after folding: no chart and no line. The page refuses a longer
  question, so in practice only the command line can send one.
- **A bare ask's transcript says two things that are not so.** It reads `-- records: not needed for this question` and
  `-- record agent: not run (the route call found no record data in the question, and no name or stored record value in
  it overrules that)`, though no route call ran. An admin reads this. The line the person sees is right.
- **A reply of only a chart mark, an image or plotting code counts as no answer.** Its line reads "this answer did not
  use record data", although data was read. The rows are not shown either.
- **A series named by a whole number moves in the page's table.** A browser lists keys such as `2024` first in any
  object, so a split by a year or a numeric picklist puts its series before the label column. Values stay under the right
  headers. The picture and the terminal keep the label first.

**On MCP,** the `chart` labels go again to the client's model, plus the `chart` key outside the 12,000-character limit
of a result, only when that client asks. That key has no limit of its own. Measured on synthetic results, not a limit:
about 4 KB for a plain chart, about 16 KB for 12 months by 8 series, about 85 KB for a line of 40 points by 8 series and
about 107 KB for 90 periods by 8 series. `result` sits beside it, so a chart cleaner does not make MCP clean: see
[MCP clients and record data](data_and_privacy.md#mcp-clients-and-record-data). Whether a real MCP client
handles `chart` or an SVG as text well is not verified.

**Reading a sidecar** (`.private/runs/<run>.jsonl`; each key is left out when it has nothing to say):

| Key | Holds |
|---|---|
| `charts` | One entry per chart: `query_id`, `tool`, `chart` (`type`, `x_key`, `y_keys`, `title`, `stacked`), `data` (the rows, equal to the result), `truncated` (true when a row of the result is not in `data`), `subtitle`, `notes`, `partial` (indexes into `data` of partial periods), `reason`, `rows_in_answer`, `alt`. Never the picture. `chart` is null, with the rows and a `reason`, when rows exist but cannot be drawn. A list, a person or a bare count gives no entry, only the line |
| `charts_talk` | True when `drop_talk` removed something on this answer. The removed text is not kept anywhere. A model test reads this key to see whether `drop_talk` does anything |
| `charts_error` | The class name of a fault `charts.make` caught, never its text |
| `charts_switch` | Why `SFA_CHARTS` could not be read |
| `status_lines` | The reason line, among any other status lines. The line is in `answer` too, and not in `answer_body` |
| `answer_kind`, `answer_body` | On a chart overrule whose merge is rejected or fails (`triage.overruled_by` `chart`, `knowledge_mode` `full`, `answer_kind` `records`), `answer_body` holds the knowledge answer and then the record answer. `record_answer` holds the record answer alone, so grade that key on such a row |
| `triage.overruled_by` | `chart` when a chart question overruled a `NO_RECORDS` route |

The rows in `charts` are the same class of value as `record_facts`, so the sidecar stays in `.private/runs/`.

**Speed.** No extra model step by design. The note adds about 0.2 s to a chart question at most, if time follows prompt
size fully, which is an upper bound and not measured. Drawing and checking take under 1 ms on the replay and a few ms on
the largest charts. A picture adds about 4.4 KB of base64 to the page's reply at the replay's median and about 10 KB at
its largest. That is no limit: a line of 40 points by 8 series adds about 94 KB, and 90 periods by 8 series about
116 KB. Every other question gets nothing added.

**The owner's decisions, with the default the build uses:**

| # | Decision | Default |
|---|---|---|
| D1 | Turn it on | On: model test and the owner's word, both 2026-11-04 |
| D2 | Show filter values on a chart | Field names only, because a value can be a person's name or model text |
| D3 | The period when none is named | Last 12 months, and the model says so |
| D4 | Keep results between questions so "now plot that" can work | No. The reply asks to name the data |
| D5 | An MCP client's model sees chart labels | Only when it sets `chart: true` |
| D6 | A second chart when an answer fetched two breakdowns | One chart, the last, with a note saying so |
| D7 | `no show chart` and `no show chart by recruiter`, which read as a refusal | No chart and no line, the same as any miss. "No show" is a likely thing to count in recruiting, so it may be worth its own rule |

**To repeat the model test,** only on the owner's word and off hours (see Rules). The questions are in
`.private/questions/charts/` (git-ignored): `asks.txt` and `plain.txt`, the same questions without chart words. One
person question is chosen at run time, so no name is written in a file; its commands are in the two files.

```bash
SFA_CHARTS=on  .venv/bin/python -m sf_assistant ask --file .private/questions/charts/asks.txt  --out .private/runs/charts_asks.txt
SFA_CHARTS=off .venv/bin/python -m sf_assistant ask --file .private/questions/charts/plain.txt --out .private/runs/charts_plain.txt
.venv/bin/python -B .private/charts/work/build/read_model_test.py   # counts only: ids, numbers, tool names, fixed words
```

Each chart is then read by hand against its answer.

## Run an evaluation

Only when the owner asks (see Rules). An evaluation competes with the page for the one model, so run it when
nobody is using the page. The evaluation runner (`src/sf_assistant/evaluation/runner.py`) replaces the notebook
and the `ask.sh --file` rounds.

```bash
.venv/bin/python -m sf_assistant.evaluation list                          # the sets, their files and sizes
.venv/bin/python -m sf_assistant.evaluation run --set all --level quick   # a fixed sample of 35 questions, merged
.venv/bin/python -m sf_assistant.evaluation run --set all                 # standard (the default level): 157 questions, merged
.venv/bin/python -m sf_assistant.evaluation run --set all --level full    # both modes where the rounds used both: 284 runs
.venv/bin/python -m sf_assistant.evaluation run --set people_dev --mode both
.venv/bin/python -m sf_assistant.evaluation run --set all --level quick --dry-run   # the plan only, no question asked
.venv/bin/python -m sf_assistant.evaluation grade --set lookup --mode records --sidecar .private/runs/<run>.jsonl \
  [--hand grades.tsv]                                                     # grade a saved run (no model call)
```

Options of `run`: `--today D` (default `sets.yaml` `today`, the day the expected answers were computed for),
`--warehouse P` (default the local snapshot). `run` exits 1 when a set/mode group failed.

- `--set`: `lookup`, `golden`, `easy`, `people_dev`, `people_holdout`, `controls` or `all`. The set files are in
  `.private/` (real names); `evaluation/sets.yaml` lists their paths only.
- `--mode`: `merged`, `records` or `both`.
- `--level`: `quick` (a fixed sample: 10 lookup, 6 golden, 4 easy and controls, 10 people dev, 5 people holdout),
  `standard` (the default: every set, merged only) or `full`.
- Grading is automatic and makes no model call. Golden questions are graded with `score_records`. People and
  lookup questions are graded with their `expected.json`; an SQL expectation is re-run read-only on the same data at
  grading time. After a data refresh, a changed count is mapped to its new value when that is unambiguous (also the
  counts of a status split, by label); otherwise, and when the SQL fails on the data, the verdict is `needs_review`
  with the reason. The answer must hold the required numbers and facts, and must have no unsupported numbers.
  Numbers inside dates, times and record codes do not count. A lookup answer whose main statement gives a different
  count is `wrong` (within 10% of the required count: `partial`, "check the scope"). Easy questions and controls get
  route checks. Each answer gets a verdict (`correct`, `partial`, `wrong` or `needs_review`) with its reasons; a
  question of a failed group is `not_run` and counts in no score.
- Output: `results.jsonl`, `report.md` and `summary.json` in one folder per run under `.private/runs/eval/`, with
  latency medians and a comparison with the previous run of the same set, level and mode (a run with a failed group
  is no baseline). The tests run it only with `mock_llm`.
- How far to trust the verdicts: on the round-2 answers the grader agrees with the hand grades on 267 of 283 decided
  (94.3%), but 62 of those labels are not real hand grades (golden and easy), and the hand grades hold only 9 wrong
  answers. On the hand-graded answers alone it is 92.8%, on people and controls 88.7%. Details:
  `.private/eval_r2_agreement_2026-10-20.md`.

The older commands still work for a single saved run:

```bash
./ask.sh --file .private/questions/records.txt
.venv/bin/python -m sf_assistant.evaluation.score_records .private/runs/<run>.jsonl          # records golden set
.venv/bin/python -m sf_assistant.evaluation.score_records --lookup <expected.json> .private/runs/<run>.jsonl
```

### One knowledge file on its own

`python -m sf_assistant.evaluation knowledge` is the notebook folder's single-object evaluation (`run_local.py`,
sections 5 to 12 of its notebook) with the same options, defaults, printed lines and output files (the deliberate
differences are listed below). It tests one knowledge file in isolation: questions built from the file (gold and
adversarial, plus model-written questions and paraphrases), answers from the knowledge agent with the knowledge tools
only, deterministic grading and the model judge, then the per-category summary, coverage (chunks no tool call
returned, chunks no passed question verifies, attribute coverage) and the knowledge gaps. Without `--dry` it is a
model evaluation: only when the owner asks.

```bash
.venv/bin/python -m sf_assistant.evaluation knowledge --dry        # the mock model: checks the plumbing, scores mean nothing
.venv/bin/python -m sf_assistant.evaluation knowledge --knowledge knowledge/objects/Session__c.json   # default: Internal_Interview__c
.venv/bin/python -m sf_assistant.evaluation knowledge --sample 40 --no-judge     # quick stratified pass, no judge
K=knowledge/objects/Session__c.json                      # a saved run is read with the file it was made with
.venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K --rerun-failed reports/knowledge_eval/<run>   # its failures again
.venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K --regrade reports/knowledge_eval/<run> --label <run>_regraded
.venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K --regrade reports/knowledge_eval/<run> --no-judge   # no model call
.venv/bin/python -m sf_assistant.evaluation knowledge --help       # every option
```

- Each run writes `reports/knowledge_eval/<object>_<YYYYmmdd_HHMMSS>/` (`_dry` added for `--dry`; `--out` and
  `--label` change it): `results.csv` and `results.jsonl` (one row per question: answer, tool calls, facts, judge),
  `summary.md`, `questions.jsonl`, `config.json` (the API key masked) and `coverage.json`. `reports/` is git-ignored:
  the files quote the knowledge text.
- The defaults are the notebook's: every question, the judge on, 2 model-written questions per category, 1
  paraphrase each of 30 gold questions, 6 workers, 8 model calls per question. The endpoint comes from `OKEVAL_*` /
  `SFA_*` or `--base-url`, `--model`, `--judge-model`. Exit 2: the knowledge file cannot be read or has errors
  (`WARN:` lines do not stop it), the saved run belongs to another knowledge file, or no question is left; exit 3:
  the endpoint is not reachable.
- `--rerun-failed`, `--regrade` and `--questions` read a saved run, which belongs to one knowledge file (its
  `config.json` names it as `knowledge_path`): pass the same `--knowledge`. With a file of another name the command
  stops (exit 2) before it loads the knowledge or calls a model.
- `--regrade <run> --no-judge` grades again with the deterministic checks only and calls no model (not even the
  connectivity check); every other run without `--dry` calls the model.
- `--knowledge` takes any path; a relative path that is not a file from the current folder is read from the
  `sf_assistant` folder. The spelling help (`SFA_RESOLVER`) is not used here.
- With `--dry`, every output file equals that of the notebook folder's `run_local.py --dry` (the parity tests in
  `tests/test_knowledge_eval.py`). Deliberate differences from `run_local.py`: the default `--knowledge`, `--out` and
  folder name above; one stderr line and exit 2 where `run_local.py` shows a traceback and exits 1 (a knowledge file
  that cannot be read or walked: missing, a folder, not UTF-8, not JSON, a section of the wrong type; no question
  left: `--qids` kept none, the `--rerun-failed` run has no failures, a negative `--sample`; no run folder is
  written then); the saved-run check; no connectivity check for `--regrade --no-judge`.

## Rebuild the knowledge

The knowledge files `knowledge/objects/*.json` and `knowledge/components/` are git-ignored. Their build, `kb_build/`, and its
inputs stay on the owner's machine, so a clean checkout has neither: copy `knowledge/` beside the code. The cli and the record
catalog read new files at their next start; the page and the MCP server load them at their next restart
(`./run.sh restart-page`, `./run.sh restart-mcp`). A flow component's development evidence is stripped when it is loaded
([data_and_privacy.md](data_and_privacy.md#flow-components-evidence-and-withheld-fields)).

## Tests

The default tests need no model and no warehouse: the data is fake, and `MockClient` stands in for the model.

```bash
cd sf_assistant
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
OKEVAL_RECORDS_LIVE=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests   # + live tests
```

The live tests open `.private/okeval_run/warehouse.duckdb` read-only (`SFA_LIVE_SNAPSHOT=<file>` points them at
another snapshot) and read `.private/questions/` and the people `expected.json`. Without the git-ignored data the
tests that need it skip and say which file is missing. `tests/conftest.py` clears every `SFA_*` alias before each
test, so an exported alias cannot change them. `tests/test_resolver.py` tests the resolver itself and
`tests/test_resolver_wiring.py` (build), `test_resolver_catalog.py` (objects, fields, values, catalog search),
`test_resolver_people.py` (names), `test_resolver_hints.py` (hints, BM25, answer line) and
`test_resolver_answer.py` (through `pipeline.answer`) its wiring, and `test_resolver_knowledge_tools.py` the knowledge
tools' arguments; each wiring test sets `SFA_RESOLVER` itself. `tests/test_tools_groupb.py` and
`tests/test_agent_groupb.py` test the Group B people fixes ([people.md](people.md)) on the fake rows of
`tests/groupb_fixture.py`; `tests/test_tools_people_fixes.py` (profile fill, one person per record, the code people
answer's same-name line) and `tests/test_agent_eval_fixes.py` (empty answers, the answer choice and its status line,
the merge's clauses, relation words) test the evaluation fixes of 2026-10-22. `tests/test_timeline.py` tests the latency logging (the timeline module, the model, tool and check entries, one answer end to end through `pipeline.answer`, and the reader on a made-up log). `tests/test_code_merge.py` tests the lean-mode merge by code (`code_merge`, the `SFA_MERGE` switch, which merge `pipeline.answer` runs, the JSONL keys and the trace line, and the rules of its review: relevance by object and field, each source on its own, people, meta text, cut-off bullets, the not-found rule, the label line and its render on the page, the planning lead, the footer, status lines and person lead read without the added bullets); a test whose subject is the merge call itself sets `SFA_MERGE=model`, and one that expects the merge by code sets `code`. `tests/test_code_tables.py` tests the record rows code writes (latency plan step 3: the `SFA_TABLES` switch, the rows of each result shape on the fake warehouse with exact texts, rows only from what the model saw, the table-line forms, drop reasons, placement and fallbacks, the record session in model mode (as before) and code mode (the note, the expanded draft checks, the marker message, the cleaners, the audit sources, the contact rule), the merges and the put-back, the JSONL keys, the last-resort guard, the trace line and the page's render, and the rules of its review of 2026-10-30: one typed row per line and only a whole copy kept, label lines between blocks, the near misses of a table line, fences in a merged text, numbered and joined table lines, a Source line written first, partial periods, one-row totals, equal headers, the draft check's own-words view, table lines in a planning sentence, and the counts read in the final answer); each test sets `SFA_TABLES` itself, and `tests/test_mcp_server.py` checks that the switch changes no MCP prompt or envelope. `tests/test_code_tables_forms.py` tests the step-3 fixes on made-up data: the written forms of a typed copy (month names, number forms, blank words, pivots, whole splits, list lines with no key), the Total line left of a typed block, `tables.restore` taking the same decision, the label line before a block that opens the answer, and the new counters and `nudges.nudge_kinds`. `tests/test_charts.py` tests the charts' pure rules on synthetic results (the `SFA_CHARTS` switch, the ask rule and its phrase lists, the choice of result and type, the payload, the truth and privacy rules, the SVG renderer and its check, `drop_talk`), and `tests/test_charts_flow.py` runs them through the real `pipeline.answer`, `result_row`, `print_outcome` and the page's `_shape` on the fake warehouse with a scripted fake of the model, against the other switches, and runs `ask --file` through to its sidecar and the model test's reader (skipped when a clean checkout has no reader); `tests/test_web.py` and `tests/test_mcp_server.py` hold the page's and the MCP server's chart tests. `tests/test_term_contracts.py` tests the business-term contracts of 2026-11-24 (candidates are Person Accounts, placement is an Offer Received client interview, support person performance is the interviews by `Interview_Support_Person__c` with its outcome split and offer rate) on the fake rows of `tests/terms_fixture.py`: the question words and their overrides, the filter code adds to each record query tool, the refusals T1, T2 and T3 and their texts, the completed support call, the code line and the cut scope claims of the answer, the merge's Counted line and the charts through a scripted fake of the model; each flow test sets its switches. `tests/test_knowledge_eval.py` runs the knowledge evaluation (`evaluation knowledge`) with
the mock model on the knowledge fixture; its parity tests also run the notebook folder's `run_local.py` with the same
options, compare every output file and pin the deliberate differences on bad input (they skip without that folder). The suites pass with the switch unset (on, the default since 2026-10-27), off and on, each also with `OKEVAL_RECORDS_LIVE=1`, with `SFA_MERGE=model` (the merge call of before 2026-10-29), and with `SFA_TABLES` unset, `model` and `code` (code-built rows, also with `SFA_MERGE=model`):

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests                    # on (the default)
SFA_RESOLVER=off PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests   # off
SFA_MERGE=model PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests    # the merge call
SFA_TABLES=code PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests    # rows by code
SFA_TABLES=code SFA_MERGE=model PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
SFA_CHARTS=off PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests     # no charts
OKEVAL_RECORDS_LIVE=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
OKEVAL_RECORDS_LIVE=1 SFA_RESOLVER=off PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q tests
```

`tests/test_web.py` runs the page on a fake pipeline with fake data. It covers good and bad codes and the cookie
flags; the sign-in throttle, also for 50 concurrent wrong codes; codes stored only as hashes, and rotation signing
everyone out; a friend never getting a trace and a friend's mode being ignored; two concurrent questions run one after
the other; the timeout path (lock held until the worker ends, late answer logged); an answer surviving a reload;
the rate limit per device; generic errors and security headers (also on the catch-all 500); the health check; the
read-copy reopen on a new inode (old session closed first, one retry after a swap error, and the reopen and retry
seconds on the ask that paid for them); and the start-up on the real code and knowledge files with no read copy. `tests/render_check.js` runs the page's renderer in node and checks
that HTML and JS stay text.

`tests/test_ask_mcp.py` runs the ask service over real HTTP on a real Unix socket with the real `Pipeline` on fakes of
the model and of `ask()`: the one tool, every row of the status table, no model call without a record session, the wait
and time limits, parallel calls that never overlap, and the real `RecordTools` on the fake warehouse to show that one
question's contact flag never reaches another. It also covers the closed reply keys, the log, the token and transport
refusals, faults that never show text, the compose file, `run.sh`, a second harness with the real `cli.ask` and record
agent on a scripted model, and `python web/ask_mcp.py` as a real process on a dead model address, also stopped with a
question in flight. `tests/test_followup.py` runs the rewrite on a fake model, with its honest list, the honest matrix (what
passes and what is lost, pinned) and the attack lists. Neither makes a model call.

The prerequisites of 2026-11-25 (small talk, display words, records retry; no new switch) have four test files, none of
which makes a model call: `tests/test_small_talk.py` (every kind in English, Hinglish and typos, the words that are
never small talk, the fixed lines with no prompt or bridge text, no model call on the page, CLI and ask service paths,
the ask service before `ensure_fresh`, no engine module on a `--no-records` run), `tests/test_display_words.py` (each
flag and shape, the log shapes as templates, Hinglish and typos, every guard, refusals, display-only and capability
asks, the display note, and the chart ask parity list `tests/golden/chart_ask_parity.json` recorded before the grammar
moved to `agent/display.py`), `tests/test_display_flow.py` (through `pipeline.answer` on the fake warehouse: no route,
names, lean, knowledge or record request holds a removed word, the chart and display notes, the chart overrule from the
flags, the typed text in the outcome and the JSONL, the invariant guard, `SFA_CHARTS=off` and records off) and
`tests/test_records_retry.py` (the forced `tool_choice` and its fallback in `LLM.chat`, the two `answer_question` hooks,
the forcing policy on every path, the clarifying question and its sources, a model fault staying `failed`, and the
replies "2", "the second one", "doosra wala" read by code). The review fixes of the same day are pinned in the same
four files (sections "review fixes"), among them every string `tests/test_charts.py` pins as no chart ask staying none
after the display split. Rollback is a revert of the change and a restart of the page, the MCP server and the ask
service; the forced tool choice has its own fallback (10 minutes off after a server refused it with HTTP 400 or 422;
a 5xx or a timeout leaves forcing on).

### The generated docs and the personal-data check

The parity checks of the restructure, which compare this package with the notebook folder it was moved from, read real
data, so they stay out of this repository with `tests/parity/`.

The generated docs must stay current (their tests fail otherwise):

```bash
.venv/bin/python scripts/gen_tools_doc.py      # docs/tools.md from tools/registry.py
.venv/bin/python scripts/gen_prompts_doc.py    # docs/prompts.md from prompts/*.md
```

When you change a prompt file's text, raise its `version:` first. `gen_prompts_doc.py` refuses to write while a text
changed and its version did not. A prompt that came from the notebook folder also needs its edits listed in
`tests/prompt_changes.py` (the prompt tests check the new text is the notebook text plus exactly those edits).

Before any commit: `.venv/bin/python .private/pii_check.py` (0 hits; a local tool, kept in the git-ignored `.private/`).

### The venv and the lock

The test venv installs the image's lock file, so the tests run the same package versions as the container:

```bash
cd sf_assistant
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --no-deps -r web/requirements.lock
uv pip install --python .venv/bin/python --no-deps pytest==9.1.1 iniconfig==2.3.1 pluggy==1.7.0 packaging==26.3 \
  pygments==2.21.0 httpx==0.28.1 httpcore==1.0.9 certifi==2026.10.18      # = pyproject.toml [project.optional-dependencies] test
uv pip install --python .venv/bin/python --no-deps -e .     # the sf_assistant package (editable)
```

After a change to `web/requirements.txt`, regenerate the lock, copy the new pins into `pyproject.toml`
(`[project].dependencies` lists exactly the lock's lines; `tests/test_packaging.py` fails while they differ), then
rebuild the image (`./run.sh up`) and the venv. Two rules: keep the versions already tested (the current lock as
constraints, `-c`; drop a line from the constraints only for the package you mean to change), and take no release
younger than two weeks (`--exclude-newer`):

```bash
cp web/requirements.lock /tmp/sfa-constraints.txt
uv pip compile web/requirements.txt --python-version 3.11 --python-platform aarch64-unknown-linux-gnu \
  -c /tmp/sfa-constraints.txt --exclude-newer "$(date -u -d '14 days ago' +%Y-%m-%dT00:00:00Z)" \
  --no-header --no-annotate -o web/requirements.lock
uv venv --python 3.11 /tmp/sfa-lock-check && uv pip install --python /tmp/sfa-lock-check/bin/python --no-deps \
  -r web/requirements.lock && uv pip check --python /tmp/sfa-lock-check/bin/python
```
