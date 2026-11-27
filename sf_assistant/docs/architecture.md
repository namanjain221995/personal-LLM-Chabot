# Architecture

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

`sf_assistant` answers questions about the Salesforce org from two sources: the **object knowledge files** (what
objects, fields, picklist values, relationships and automation mean, with the **flow component files** beside them:
one file per production flow) and a **read-only DuckDB copy of the warehouse** (the records: preprod sandbox
data). The local main model (`Qwen/Qwen3.6-35B-A3B-NVFP4`, served by vLLM
at `http://localhost:8000/v1`) makes every decision through typed tools. Code checks every object, field and value
the model picks and builds every SQL statement with bound parameters. **The model never writes SQL.**

Status on 2026-10-20: stage 1 (the notebook pipeline moved into this package) and stage 2 are done: the cleanups, the
page's switch to this package, the MCP server (container `sf-assistant-mcp`), the evaluation runner, and the fixes of
the stage-2 review. Stage 3 wires the fuzzy term resolver ([resolver.md](resolver.md), plan
[resolver_integration.md](resolver_integration.md)): its matcher and the knowledge part of its vocabulary moved to
`knowledge/resolver.py` and `knowledge/terms.py` (decision A1), and it is built once per run behind the switch
`SFA_RESOLVER` (on by default since 2026-10-27; `SFA_RESOLVER=off` turns it off). With the switch on, the question hints
of both agents, the BM25 searches, the record tools, the people tools and the answer's "Read as" line use it; with it
off every path is the old one (status: [resolver_integration.md](resolver_integration.md)).

Flow components (stage 4 of the flow build, 2026-10-24): `knowledge/components/` holds one JSON file per production
flow (`flows/<ApiName>.json`, built by `kb_build/flows`) and a generated `index.json` with the reverse
links. `knowledge/components.py` loads and checks them, and strips the files' development evidence when it reads
them. `tools/component_tools.py` gives the full knowledge agent three more tools (`search_components`,
`get_component`, `find_usages`; [tools.md](tools.md#flow-component-tools)), and its system prompt then ends with the
components note (`prompts/knowledge_components_note.md`). The page and the MCP server copy the folder at start with
the object files. The lean knowledge step and the one-object registry do not get the component tools. The object
files are unchanged; where an object file's automation entry and a component disagree, the component's state and
start are current (the note says so).

Related pages: [tools.md](tools.md) (every tool, generated), [prompts.md](prompts.md) (every prompt id and version,
generated), [data_and_privacy.md](data_and_privacy.md), and [runbook.md](runbook.md). The design specs `SPEC.md` and
`RECORDS_SPEC.md`, which the code's "RECORDS_SPEC §..." references cite, stay out of this repository.

## Diagrams

The design diagrams (a high-level architecture and a low-level design) stay out of this repository.

The diagrams show the target design. Where the code differs today:

| In the design | In the code |
|---|---|
| `server/app.py`: MCP server, stdio and Streamable HTTP `:8700` | `server/app.py` (Python MCP SDK 2.3.0, low-level `Server`), `server/data.py` (knowledge copy, record tools on the read copy), `server/__main__.py` (`python -m sf_assistant.server --stdio / --http / --health`). `server/knowledge_mcp.py` is the notebook's one-file knowledge server, kept |
| `server/auth.py`: OAuth 2.1 resource server | No OAuth. `server/auth.py` holds the MCP server's bearer tokens, stored only as SHA-256 hashes in `.private/mcp_token.json`, rewritten in place. The page uses its two access codes, kept as scrypt hashes (`web/codes.py`) |
| `server/policy.py`: per-user policy, row filters, rate limit | No policy module and no row filters. The field blocklist and sensitive objects are in `engine/catalog.py`, enforced by `engine/sql_guard.py`. The page has its own rate limit; the MCP server limits each client to 30 tool calls and 120 resource reads and prompt gets per minute |
| `server/audit.py`: audit table, OpenTelemetry | A JSONL file, no table: `server/audit.py` appends every MCP call to `.private/mcp_log.jsonl` (keyed hash of the arguments, no values). The page logs to `.private/web_log.jsonl`. No OpenTelemetry |
| `tools/actions.py`: write actions | Not built: everything is read-only |
| `resources.py`: knowledge as resources | Inside `server/app.py` (`sfa://object/{api}`, `sfa://freshness`) |
| `knowledge/graph.py` | Not built. Links between objects come from the knowledge tools (`find_links_to_object`, `get_relationships`) and the catalog's lookup index |
| `engine/executor.py`, `engine/name_index.py` | `engine/warehouse.py`, and `tools/names.py`. The name index is state of `RecordTools` because it needs the warehouse connection |
| `agent/pipeline.py`, `router.py`, `record_agent.py`, `knowledge_step.py` | `pipeline.py`, `agent/triage.py` with `RecordSession.route` / `triage`, `agent/record_session.py`, and `agent/lean.py` with `agent/knowledge_agent.py` |
| Limits: 200 rows, 15 s per query, 30 calls per minute | Record tool result at most 12,000 characters, 10 s per SQL statement, 20 s per tool call. The page allows 20 questions per device per hour |
| Live Salesforce API path | Not in this package: answers come only from the knowledge files and the warehouse copy |

## Where things run

```
 LAN (ufw allow-list)                         this machine (SFA_BIND, SFA_MCP_BIND)
 ─────────────────────                        ──────────────────────────────────────────────────────────────────
 browser ── HTTP :8600 ──────────────────────> sf-assistant-web   (container, network_mode host, read-only rootfs)
 MCP client ── HTTP :8700/mcp + bearer token > sf-assistant-mcp   (container, network_mode host, read-only rootfs)
 orchestrator ── Unix socket, volume ─────────> sf-assistant-ask   (container, network_mode host, read-only rootfs;
                  (later stage, not connected)    no TCP port, one tool, one question at a time)
 Claude Desktop / IDE ── stdio ──────────────> docker exec -i sf-assistant-mcp python -m sf_assistant.server --stdio
                                               (or .venv/bin/python -m sf_assistant.server --stdio: local snapshot)
                                               ./ask.sh  or  python -m sf_assistant ask   (terminal, .venv)
                                                   │
           ┌───────────────────────────────────────┼─────────────────────────────────────────────┐
           v                                       v                                             v
 vLLM localhost:8000/v1            /data/warehouse.read.duckdb                 .private/okeval_run/warehouse.duckdb
 (sf-local-ai stack;               (volume sf-local-ai_data, mounted :ro;      (local snapshot for the cli and tests:
  not part of this project)         the sync-worker replaces it after           a docker cp of the read copy,
                                    each sync; the containers read it)          --refresh-warehouse)

 /data/warehouse.duckdb: the sync-worker's writer file. Nothing here opens it, and every path check refuses it.
```

- **The page** (`web/`, compose project `sf-assistant`, container `sf-assistant-web`) runs one question at a time
  for its signed-in users. Since 2026-10-20 it imports this package (`sf_assistant.cli.ask`), its prompt and
  knowledge files from the sf_assistant folder, mounted read-only at `/srv/sfa` (`SFA_HOME`); before, it imported
  `ask.py` from `notebooks/object_knowledge_eval`. It reads the live read copy, and before each question it reopens
  on a new copy when the file's inode, mtime or size changed.
- **The MCP server** (`src/sf_assistant/server/app.py`, container `sf-assistant-mcp` since 2026-10-20) offers the
  same tools the agents use: the knowledge tools (one object and several objects, from `tools/registry.py`) and the
  14 record tools. It also has `list_resources` (`sfa://object/{api}` knowledge JSON, `sfa://freshness`) and `list_prompts` (answer
  templates). It is read-only, takes record data only from the read copy, and applies the field blocklist as the
  record tools do. Transports: stdio (`python -m sf_assistant.server --stdio`), and Streamable HTTP on
  `SFA_MCP_BIND:SFA_MCP_PORT` (default `127.0.0.1:8700`) with a bearer token. The token comes from
  `run.sh new-mcp-token` and is stored only as a hash in `.private/mcp_token.json`. The server runs as a second
  compose service, `sf-assistant-mcp`, which of `.private/` mounts only its token file, its audit key and its log.
  The MCP server runs tools only and makes no model call; the client brings its own model (see
  [data_and_privacy.md](data_and_privacy.md#mcp-clients-and-record-data)). With `SFA_CHARTS=on` at its start,
  `group_records`, `trend_records` and `compare_records` take an optional `chart: true`, and the envelope gets a key
  `chart` beside `result`: code draws it, and there is no new tool ([runbook.md](runbook.md#charts-sfa_charts)).
- **The ask service** (`web/ask_mcp.py`, compose project and container `sf-assistant-ask`, built 2026-11-04, not started)
  offers one MCP tool that runs the full pipeline for the platform's `/chat`. See [below](#the-ask-service).
- **The cli** (`./ask.sh`, `python -m sf_assistant ask`) runs on the host from `.venv` against the local snapshot.
  It prints the answer, the RECORDS block (every query with its SQL, bound params and rows) and, with `--file`,
  writes a transcript and a JSONL sidecar to `.private/runs/`.
- **The evaluation runner** (`python -m sf_assistant.evaluation run --set ... --mode ... --level ...`) replaces the
  notebook and the `ask.sh --file` rounds: it runs one `python -m sf_assistant ask --file` per set and mode, then
  grades every answer with no model call. See [runbook.md](runbook.md#run-an-evaluation).

## The ask service

`web/ask_mcp.py`: one MCP tool, `ask_salesforce`, on `Pipeline.from_home` (the page's wrapper) and the MCP server's
`TokenStore` and `http_app`. It is its own container for two reasons: a tool on 8700 would give every token of the MCP
server a model path and contact fields, and `Pipeline` lives in `web/`, above the server in the layering
(`tests/test_layering.py`). It has **no TCP port**, only the Unix socket `/run/sf-assistant-ask/ask.sock` in the named
volume `sf-local-ai_ask-sock`: a port on the docker bridge would be open to every bridged container and perhaps the LAN,
and idle connections there could fill the server's limit with no token. Measured on a dead model address on 2026-11-04,
with the socket: `pipeline ready` in about 11 s, process peak 817 MB, container peak 833 MB (container peak 915 and
1,123 MB in earlier runs, with more page cache, which the kernel can take back, and tmpfs), about 50 threads; real answers
are not measured. `pipeline.answer` is unchanged, and the page and the MCP server do not change. Layer: web, with the
page.

**Input** (`additionalProperties: false`): `question`, at most 1,000 characters; `recent_turns`, at most 4 items
`{role: user|assistant, content}`, each at most 2,000 characters; `request_id`, a string. Only `question` is required. No
user, role or chat id crosses. A role other than user or assistant is dropped and sizes over the limits are cut, not
refused; arguments of another shape get `ok: false`.

**The call**, with no initialize and no MCP library, gives a JSON reply on one connection to the socket:

```
POST http://sf-assistant-ask/mcp           (httpx: transport=httpx.AsyncHTTPTransport(uds="/run/sf-assistant-ask/ask.sock"))
Host: sf-assistant-ask
Authorization: Bearer <token>
Accept: application/json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ask_salesforce","arguments":{...}}}
```

Wrong token 401, any other Host 421, an Origin header 403, another Accept 406, no token file 503. A reply is
`result: {content: [{type: "text", text}], isError}`, and `text` is the envelope `{ok, result, error, chunk_ids}`, the
shape of the MCP server's tools. `ok` is false, with `isError`, only for an unknown tool or arguments of the wrong shape.

**`result`** always has exactly these keys: `status`, `answer`, `kind`, `data_ts`, `objects` (the objects of the
record queries that worked, each once) and `charts` (a closed set of keys per item: `chart`, `data`, `truncated`,
`subtitle`, `notes`; no picture). `answer` is one joined text, in this order: ``Read as: `<rewritten question>` `` when the
rewrite changed the question, as a code span so that nothing in it links (no backtick and no format character in it; the
contact rule of an answer applied); the body, then the Source line (`_shape` takes it out, and the page puts it back under
the answer), both with `](` and `]:` escaped, so that Markdown reads no link, image or reference in them and a link planted
in a record cannot hide its target (a code span shows the backslash), and with
every form the frontend's citation filter and Markdown would change written so that the number shows as stored: `[12]`,
`*` or `~` between digits, `2 *3* 4`, the `.` of `3 .5` and `&#49;` escaped outside code; a number in a list that does not
run on from the one before (`12.` then `15.`) given the other delimiter, which starts a new list at its own number; an
indented code block written as a fence and a two-backtick or multi-line code span as a one-line span, which show the same;
a `[12]` in a bare URL percent-encoded, which links the same. Copy gives the written form (`web/ask_mcp.py`,
`_numbers_as_stored`);
`No record data was used for this answer.` for kind `knowledge`; on status `refused`, `One blocked field was left out.`
Never sent: the trace, the log record, SQL, parameters, the model address, raw error text, or
`_shape`'s placeholder `No answer was found for this question.`

**Status**, the first row that fits:

| # | Condition | `status` | `answer` |
|---|---|---|---|
| 1 | no letter or digit in the question (empty, or zero-width characters, a NUL or a BOM alone), or over 1,000 characters | `failed` | empty |
| 1a | small talk (`agent/small_talk.py`: a greeting, thanks, "are you there", "can you tell me one thing"; new 2026-11-25, P6), before `ensure_fresh()` and also with records off: no model call, reason `small_talk` | `answered` | the fixed line |
| 2 | `pipeline.session` is None after `ensure_fresh()`: no model call | `failed` | empty |
| 3 | the rewrite call raised, or the endpoint is not local: the typed words are not sent on | `failed` | empty |
| 4 | the rewrite says UNCLEAR to a question that is not complete or has no subject of its own, or echoes a fragment, or fails the output rule, or `asks_for_contact(rewritten) != asks_for_contact(raw)`, or adds a phone number, or is not bound to the typed question (`agent/followup.py`) | `unclear` | `I could not tell what this refers to. Please ask the full question, with the name or object.` When the model's UNCLEAR named what is missing (`UNCLEAR missing=number,period about=...`, the codes who, object, number, period; development B of 2026-11-27), that line comes after one line built from `prompts/followup_unclear.md` and the person's own typed words, e.g. `Fewer than how many interviews, and which years do you mean by "last years"?`, with `Do you mean <words of the earlier question>?` first when the reading is a run of an earlier user turn as typed (`followup.unclear_text`). The fixed line always stands in full: the platform bridge looks for it (`sfk_bridge.follows_a_stop_line`). The same text in off, shadow and on (the resolver's `"missing"`) |
| 5 | `Pipeline.answer` raised, or any other fault in the service, or a reply JSON cannot hold (a NaN, a key that is no string) | `failed` | empty |
| 6 | any `trace.queries[].error_code` is `E2c` or `E1d` | `refused` | kind `merged` or `records`: the real text, joined, then `One blocked field was left out.` Any other kind, or no text: `This field is blocked, so it cannot be shown here.` alone |
| 7 | kind `merged` or `records` | `answered` | joined |
| 8 | kind `knowledge` and (`trace.records` is `off` or `error`, or `trace.record_error`) | `failed` | empty |
| 9 | kind `knowledge` otherwise | `answered` | joined |
| 9a | kind `none`, `trace.clarify` and text: the clarifying question code wrote when the record step got no data (`agent/clarify.py`; new 2026-11-25, P2), reason `clarify`, though `trace.record_error` is set | `answered` | the text |
| 10 | kind `none`, and `trace.error` or `trace.record_error`, also with text (a chart's reason line during a model failure; rows read, then an empty reply) | `failed` | empty |
| 11 | kind `none` with text and no fault (a chart question that names nothing to chart, a question of display words only; reason `code_line`, or `small_talk` for small talk `Pipeline.answer` itself answered) | `answered` | the text |
| 12 | kind `none`, no text | `no_answer` | empty |
| - | 120 s in line | `busy` | empty |
| - | 240 s of running | `timeout` | empty |

**A job**, in the one worker thread: `ensure_fresh`, the session check, the rewrite (`agent/followup.py`),
`Pipeline.answer(q, "merged")`, the rows above. `agent/followup.py` makes no call when no turn is left after the filter,
so a first message runs exactly what the page runs. It reads the reply in this order: the typed question again (the same
words, whatever the case, the punctuation or the line breaks): the typed question, unless it is a fragment (a lead and,
aur, same, what about or how many with only names, periods and pointing words after it, such as `same for Golf Vofir` or
`how many last week?`; or, with no typed name after the first word, where a capital alone is none (`his Email`, `can I`,
`Ok. What`, `What Is His Email?`): a word that points back, such as a pronoun, `that person`, `that status`, a last `it` or
a Hinglish `dono` or `wahi`; a lead and, aur, same or what about; or a contact ask that names nobody), but a complete question
behind a lead, on another subject, is none (`and what is the leave policy?`); a complete typed question (it names its own
subject and has no word that points back; `this week` and `and` do not; and it does not lean on the turns by its shape, as
`how many were rejected?`, `kitne reject hue the?`, `who else joined?` or `who hosted the interview?` do) with only its
spelling, question frame or word order changed, which runs as typed too (`who is the manager of foxtrot persn`, `Foxtrot Persn
ka manager kaun hai?` respelt in English), never `unclear`, and only the typed text goes on; the same for `UNCLEAR` alone
for a reply, only when that question also says who it is about or asks how the org works (`Foxtrot Persn ka mobile number?`,
`what is the leave policy?`), because a model `UNCLEAR` is no proof of anything else: a question with no subject of its own
stays `unclear` on it, also a complete one (`How many candidates joined this quarter?`); no answer (`UNCLEAR` alone in any
dress to any other question, no letter or digit); the output rule (one line, at most 1,000 characters, not cut off); the contact rule; bound to the typed
question, around one idea: a user turn is trusted more than an assistant turn, because stored record text arrives in
assistant turns. So no format character is added, a scope word is compared one by one with the typed question and the user
turns, no typed word is lost (only pointing words and question words such as `what is` or `kya hai` may go), a typed question
of 4 or more words with no back-pointing word must come back as typed unless it asks for one field of nobody (`what is the
joining date?`, `what is joining date?`, `joining date kya hai?`, never `what is the leave policy?`) or a count of nothing, a typed question of only
pointers must get a question back, a reply that still only points (`what about her Echo One?`) is refused (`no_topic`), a
rewrite that counts or lists, or that goes on from a typed `them` or `same for`, must keep the period of the last user turn
or of its `Read as:` line unless the typed words give their own, `so far` or `by month`, because a count that loses `last
month` runs over all time (`period_lost`), a typed period replaces the old one and a reply that holds both is refused unless
the typed words compare them (`period_kept`), a rewrite that goes on from a last question that names people or records must
hold them or a person the rules below let in (`person_lost`), because `March mein kitne offers reject hue?` for `aur kitne reject
hue?` counts over everybody, and so does `kitne offers reject hue?` after a question about `REQ-0042`; a typed `his` needs one
of the people named, anything else all of them. The typed words may name their own subject instead (a typed name, a record
number, a word in a name's place as in `bufa ka email?`, `manager of bufa?`, `bufa's team?` or `when did bufa join?`, or a
typed word the reply puts in a name's place), leave the person on purpose (`total`, `overall`, `everyone`, `sab`, `kul`, `who
else`, `across the company`, never `list`, `show all` or `each month`), or ask of something else (a complete question behind a
lead that shares no word with the last question, or one on how the org works), but a typed pointer goes first (`what are his
total applications?`, `uske manager ka email?`), and a role, a field, a status, a date or an amount is no subject of its own
(`candidate ka email`, `how many on 15th?`, `how many were Rejected?`). The person is read from the capitals of the last
question, without titles, fields and abbreviations (`Mr Echo`, `Echo One's Manager`, `HR`, `I`), from a word in a name's place
(`Tango ne ...`, `did jonas make ...`) and from the lead when it repeats a name of two words, never from a label
(`Joining Date: ...`), and a value a typed word replaces is not kept (`in Pune`, `and in mumbai?`). A word of the last question
or of the typed words may come back in its past when the first sentence of the last answer holds that form too (`made` after
`make`, `logged` after `log`: a spelling rule and a short table, one way only, so a typed `paid` does not open `pay`), and
words of an assistant turn enter only as one capitalised name of at
most 4 words, never a word for many records (`Candidates`) or a word mixing ASCII with letters of another script. That name
must stand, as written, in the lead of the last answer (its first sentence or its `Read as:` line), because notes, table
rows and a forged `User:` line come after it (`outside_the_lead`), and it may not be one of several new names in that
sentence unless a typed word such as `first` says which (`several_names`). It must also be what that answer offers. Nothing
in brackets or in a sentence with a note or a request counts, unless the last question holds that word itself, nor a name in a
clause of its own that says nothing of the last question (`No manager on file, go to Golf Victim.`) (`in_a_note`).
The kind of the last question decides, which no record text can change (`not_the_result`): a name can be the result of
`who manages Echo One?`, `which client ...`, `kaun`, `what is the manager of Echo One?` and of a `show` that names no person,
a record number can be what a typed `first one` picks, and a value what a typed `that source` means, but no name is the
result of `when did Echo One join?`, `how many ...` or `did Echo One ...`. A typed question that points at nobody (`what is
the designation?`) stays with the person the last question named, also after `who manages Echo One?`. A typed `his` or `uska`
moves on to the answer: after a last question that asked for a person (not `who is Echo One?`, not `which client ...`) and an
answer that gave one, a reply that goes back to the person the question named is refused (`skipped_the_result`), because a name
the user typed passes every other rule and would open contact fields of someone nobody meant. A person of an older user turn does not replace the person the last question named (`earlier_person`). A `Read as:`
line that lacks the typed words of the user turn before it is stored text, not the question that ran.

Code cannot catch every rewrite. The field block and the contact rule still apply to the question that runs, `Read as:` shows
it, and only admins reach the service. The read copy can also break while the rewrite runs: the knowledge step then makes its model calls with
no record session, no record is read, and the status stays `failed`.

## One question, end to end

Merged mode (records on, the default of the cli and the page). `sf_assistant.pipeline.answer` does this; the
cli's `ask()` and the page wrap it.

0. **Code first, no model call** (the prerequisites of 2026-11-25). **Small talk** (`agent/small_talk.py`, P6): a
   greeting, thanks, an acknowledgement, a goodbye, "are you there", an empty ask ("can you tell me one thing") or a
   help ask, read as a full match of a word table (English, Hinglish, typos; never yes or no, a digit, an Id or any
   other word), gets one fixed line of `prompts/small_talk.md` and stops (`line_reason` "small_talk"). **Display
   words** (`agent/display.py`, P3): `split` takes how to show the answer (chart, graph, plot, visualize, artifact;
   table, dashboard, detail, summary, report, breakdown, excel, csv only in a display shape) out of the question and
   returns the search text and flags. Guards keep a word inside quotes, an API name, a question about the thing
   itself, a flow or org chart, a grouping ("breakdown by"), a gated word outside its shapes ("the dashboard",
   "details of", "reports to", "candidates who know excel"), a stored record value or a stored person's name (a
   value that is only the file word of a file shape, a company named "Excel" in "send X to excel", keeps the word and
   still sets the file flag); when the search text would change what the contact rule or the term contracts read, or
   lose a number or a quoted span, the split is dropped (the flags stay). The search text feeds the spelling help,
   the knowledge step, the route and names calls, the value and people spots, `begin_question`, the hints and the
   draft checks; the typed text stays in the outcome, the merge, the contact rule, `charts.make`, the JSONL and,
   with `SFA_CHARTS` on, the record agent's user message (pre-release fix of 2026-11-25: read as "show X of last 6
   months", "plot X of last 6 months" got a record list instead of a monthly trend with a chart; off, the record
   agent reads the search text). A question of display words only gets one line
   (`prompts/display_words.md`: the capability line for "can you do visualizations ?", told apart for a file, a
   dashboard and `SFA_CHARTS=off`, else the bare line). Review fixes of 2026-11-25: a refusal the chart grammar reads
   ("hide the pie chart", "stop showing charts") refuses the chart and keeps its words, "visually impaired" or
   "visualization engineer" stay as typed, and `pipeline.chart_reading` gives the chart steps one reading, so no
   string the chart tests pin as no chart ask becomes one; a small-talk name after "this is" / "I am" is capitalised
   or has a tail and is never a state or record word ("hi, this is about interviews" goes on); a knowledge answer loses
   a sentence that echoes the require-tools nudge (`pipeline.drop_nudge_echo`).

1. **Triage**, in `RecordSession.route` / `triage`. The **route call** (`route_system`) decides whether the
   question needs record data: `RECORDS` or `NO_RECORDS`. Next to it, code spots stored person names (the name
   index in `tools/names.py`), and one small **names call** (`names_system`, 60 tokens) reads the names in the
   question. A question that names a stored person, or a name no stored person has, goes to the records whatever
   the route said. After the people are decided, code also spots the stored record values the question names (the
   **value spots**, `tools/value_spots.py` over the catalog's entity index: Names, codes and short text of every
   queryable object; [value_spots.md](value_spots.md)). A strong one sends a `NO_RECORDS` question to the records
   too (`Triage.overruled_by` "value"); a picklist value, a record type name or a plain word never does.
2. **Knowledge step and record agent, at the same time** (a thread pool of one).
   - Route `RECORDS`: the **lean knowledge step** (`agent/lean.py`, `knowledge_lean_system`). It gets 5 read tools,
     at most 3 tool calls and 3 model calls, and returns a short facts block on what the objects, fields and values
     mean. Route `NO_RECORDS`, an unclear route, or a `NO_RECORDS` route a stored record value overruled: the full
     **knowledge agent** (`agent/knowledge_agent.py`, at most
     8 model calls, every tool result cut to 16,000 characters). With every knowledge file loaded it uses the
     multi-object tools and `multi_agent_system`; when `knowledge/components/` holds an `index.json`, also the 3
     component tools, and its system prompt ends with `knowledge_components_note`. Their results stay well under the
     cut: a search at most 4,000 characters, a component summary at most 3,500, its effects (one line each),
     logic, fields and caveats in pages of at most 6,000, a usage listing at most 40 rows and 9,000 characters.
   - The **record agent** (`RecordSession.ask`, `records_agent_system` filled with the live status line) gets at
     most 12 tool rounds over the 14 record tools. Code reads the structured keys of every tool result and sends
     **nudges**: lookup checks, overlaps, breakdowns, date coverage, mirrors, unsynced fields, people, shared names,
     no data (`agent/nudges.py`, `agent/people_answer.py`). When the agent gives no answer about a named person,
     code writes the record part from `about_people`. Its question hints list the records the question names: where
     each is stored, its record type, whether a description is stored and which records link to it, with counts; a
     `NO_RECORDS` draft about such a record gets one pushback (`records_value_no_records_message`).
     **Code rows** (`SFA_TABLES=code`, latency plan step 3; the default `model` leaves the rows to the agent): the
     agent's system prompt ends with `records_tables_note`, so it writes 1-3 sentences, a table line per result to
     show (`{{table q3}}`) and one bullet on what was counted; right after the reply's cleaners code writes that result's rows from
     what the model received (`agent/tables.py`: a markdown table for `list_records`, `- label: value` lines for
     groups, periods, segments and a breakdown), and the draft checks, audits and contact rule read the answer with
     its rows ([runbook.md](runbook.md#how-record-rows-are-written-sfa_tables)).
     **Display note** (P3): a table, detail, summary, breakdown or file flag appends `records_display_note` (one lead
     line and one line per flag) after the chart note.
     **Records retry** (P2): a records-routed or chart-overruled question that names nobody gets ONE forced round
     (`tool_choice` "required", `records_tools_forced_message`) when its draft ran no record tool: at the require-tools
     nudge, or at the first draft check, and with `records_all_failed_message` when every call failed (never after a
     not-found or refusal code). A `NO_RECORDS` draft is forced only after a `RECORDS` route and never on a chart
     question. `LLM.chat` sends the forced choice and, when the server refuses it (HTTP 400 or 422), sends the call
     again at once as "auto" and keeps forcing off for 10 minutes; a 5xx or a timeout takes the normal retries with
     the forced choice kept. With no tool run, the draft check asks for the record tools instead of "the final answer
     from the tool results above". Review fixes of 2026-11-25: an engine fault (E10, E12, E13, internal) gets no forced
     round and no clarifying question (the record step fails, so the caller's fallback runs); numbers after only
     `describe_fields` or the sync status get the forced round with `records_run_query_message`; the forced round
     counts no draft, so the next draft gets the full check (the number audit); once it is spent no second retry
     message goes; and a forced `NO_RECORDS` draft whose round read no data keeps its `NO_RECORDS`.
3. **Record tool call**, in `tools/record_tools.py` and its mixins. The arguments are validated against the
   tool's JSON schema (E11 codes). The catalog grounds every object, field, relationship and lookup role, and
   refuses an unknown one with an error code (E1 object, E2 field, E2b documented but not synced, E2c blocked) and a
   hint instead of a guess (`engine/catalog.py`). The
   model's value becomes the stored spelling, or error E4 with the stored values (`engine/values.py`; with the
   spelling help on, the default, one clear close spelling of an object, field or value is used inside a tool call
   and stated, [resolver_integration.md](resolver_integration.md) (3)-(4); with `SFA_RESOLVER=off` nothing fuzzy is
   bound automatically). A period
   in words becomes a date range in the business time zone (`engine/periods.py`). `engine/query_builder.py` builds
   the statement with `?` parameters. `engine/sql_guard.py` parses it with DuckDB and accepts exactly one SELECT on
   allowed tables, with no `*`, no table functions and no blocked column. `engine/warehouse.py` runs it on a
   read-only connection with external access off and a 10 s timeout. Every call is logged as `QueryEvidence`
   (arguments, what was applied, each SQL with its params and rows, the tables and their sync times). The SQL never
   goes to the model. A result is at most 12,000 characters of JSON.
4. **FIELD CHECK.** When a person count used a lookup whose role does not fit the question's verb, and the
   knowledge answer names the lookup that does, code adds a FIELD CHECK line for the merge (`nudges.field_checks`).
5. **Merge.** When the record agent used records, both answers become one (`agent/merge.py`).
   - Lean step, no FIELD CHECK line (since 2026-10-29, `SFA_MERGE=code`, the default): **code** keeps the record
     answer as it is and adds at most 3 bullets of the facts block before its Source line, with no model call
     (`code_merge`; after a list the label line of `prompts/code_merge_label.md` first). A bullet is kept only when it
     is about a field the answer used, by object and field (the queries' `fields_used`, "applied" names and lookup
     checks, the record answer's names; a people answer's lookups), or, naming no field, about the counted object,
     and passes the merge's checks (the number audit and its not-found rule, working notes and tool talk, the scope
     check) and the review's (no person of the question, no talk about the knowledge, the question or the prompt, no
     cut-off bullet); none kept: the record answer is shown, with the counted line below when code added one
     ([runbook.md](runbook.md#how-answers-are-merged-sfa_merge)).
     The step-2 fixes of 2026-10-30 ([runbook.md](runbook.md#the-step-2-fixes-2026-10-30)): the merge by code gets the
     record catalog's objects (name -> spec) and drops a bullet that contradicts the catalog or the knowledge (a lookup
     target, a field the object lacks, a picklist called a lookup, the row of an object no lookup links to the answer's
     objects, a legacy or FLS claim the knowledge does not make, a value meaning the knowledge does not give, a legacy
     note under a count that used a mostly filled field) or the answer's
     own results (a value count or "unused" claim, a caveat the lookup check resolves), and one whose subject is
     another object; the unfinished-end test reads the facts block's last line only, and the cap puts a bullet on a
     counting query's date field first. When the one counting query's grouped field, blank group, not-blank filter or
     list total is missing from the record answer (and its kept bullets), one code line says what was counted
     (`prompts/code_merge_counted.md`); never after a people answer code wrote. A Hinglish record answer gets nothing.
   - Otherwise (the full knowledge agent, a FIELD CHECK line, a record answer that still plans aloud, thinks aloud,
     reads as tool talk or plans aloud in Hinglish, or `SFA_MERGE=model`): one **model call** (`combine_system`, `combine_user`, `combine`) merges both answers with the
     RECORD FACTS (at most 16,000 characters) and the FIELD CHECK lines. With code rows the merge call gets the record
     answer with its rows as table lines (`combine_tables_note` appended to its system prompt), every check still
     reads the rows, and code puts them back into the merged text (`tables.restore`); the merge by code keeps them
     verbatim.
   **Number audit:** code rejects a merge when one of its numbers is not in the record facts, the question, the
   knowledge answer or the status line (`checks.audit_numbers`), or when the merge changes the scope or calls an
   object "not available" that the warehouse holds. A rejected merge falls back to the record answer.
6. **Code footer.** When the answer does not name the lookup its person count used, code adds a "Lookup used: ..."
   line (`with_lookup_footer`), and it adds a "Source: <origin>, synced <time>." line when the answer has none
   (`with_source_line`). The texts of both lines are prompt files (`records_footer_*`, `records_source_line`).

**Charts** (`SFA_CHARTS=on`, the default since 2026-11-04, [runbook.md](runbook.md#charts-sfa_charts)). On a question that asks
for a chart, code works beside these steps and adds no model step of its own; a `NO_RECORDS` route it overrules runs the
record agent as well ([runbook.md](runbook.md#charts-sfa_charts), Limits). A bare ask (`now plot that`) stops before
step 1 with
one line and no model call. In step 1 a `NO_RECORDS` route is overruled (`Triage.overruled_by` "chart") and keeps the
full knowledge agent. In step 2 the record agent's system prompt ends with `records_chart_note`, and it keeps its 14
tools. After the merge and the contact rule, before the code footer's lines, `charts.make` reads the record results
(`QueryEvidence.model_result`), picks the last chartable one, draws one SVG and checks it, or gives one reason line that
goes before the Source line. `Outcome.charts` reaches the page's job reply, the cli's text and the sidecar (with no
picture).

**Clarifying question** (P2, `agent/clarify.py`). When the record step of a record question ran no record tool, or
every call failed, after its forced round (`RecordAnswer.no_data`) and no model fault ended it, code writes a question
back instead of "the figures could not be computed": a lead line, 2-3 numbered choices and a closing line
(`prompts/records_clarify.md`), kind `none`, no chart and no line. The choices come from the business terms of the
question, the objects it names, the objects of the failed calls, the objects of the last answer (`context_objects`,
from the ask service and the page's follow-up path) or fixed examples, each written with the question's display words
and period. A reply "2", "the second one", "doosra wala" or the choice's text is read by code in `followup.rewrite`
(`clarify.choice_reply`) and answered as that choice.

The result is an `Outcome`. `kind` is `merged`, `records`, `knowledge` or `none`. `body` is the answer without the
code footer, which is what scoring reads. `final` is the answer with it.

Other modes: `--records-only` (page: "records only") runs the record agent alone, with no merge. `--no-records`
(page: "knowledge only") runs the full knowledge agent and imports no record module and no duckdb. With `SFA_CHARTS=on`
every answer imports `agent/charts.py`, which imports the record modules only inside its functions, so such a run still
loads none of them. A chart question there gets the `records_off` line through `agent/nudges.py`, which loads
`engine/types.py` and no duckdb.
`--full-knowledge` runs the full knowledge agent for record questions too. Records are only on with a model endpoint
on this machine (`is_local_endpoint`: `localhost`, `127.0.0.1`, `::1`). With any other endpoint they are off,
because record data must stay local.

## Module map

Imports go one way: config/prompts < knowledge < engine < tools < agent < evaluation/server < pipeline < cli <
`__main__` < web. `tests/test_layering.py` checks every import statically: every import kind, no import cycle at
import time, and the knowledge side (what came from the notebook's `okeval`) never imports record code. A
`--no-records` run loads no duckdb. No package `__init__` re-exports anything. The design specs of the notebook
folder, which stay out of this repository, name the old paths.

| Layer | Module | What it does (was, in `notebooks/object_knowledge_eval`) |
|---|---|---|
| 0 | `config.py` | Paths (`HOME`, `KNOWLEDGE_DIR`, `COMPONENTS_DIR`, `PROMPTS_DIR`, `QUESTIONS_DIR`, `.private`), `Limits` / `LIMITS`, `Config` (model endpoint, knowledge agent), `RecordsConfig` (snapshot, time zone, today, query limits), one `Settings` per run (`load_settings`), the merge switch read at each answer (`merge_settings`, `SFA_MERGE`), the table switch read at each answer (`tables_settings`, `SFA_TABLES`, its default the one constant `TABLES_DEFAULT`), the chart switch read at each answer (`charts_settings`, `SFA_CHARTS`, its default the one constant `CHARTS_DEFAULT`), `ENV_DOC`, `SFA_*` aliases (`okeval/config.py`, `okrec/config.py`) |
| 0 | `prompts.py` | Loader of `prompts/<id>.md`: `load`, `text`, `ids`, `versions`, `fill`; checks each file once ([prompts.md](prompts.md)) |
| 1 | `knowledge/store.py` | `ObjectKnowledge`: load, validate and index one object knowledge JSON; `knowledge_files` (`okeval/knowledge.py`, `okeval/multi.py`) |
| 1 | `knowledge/search.py` | BM25 search over knowledge chunks (`rank_bm25`; the page refuses the built-in fallback) |
| 1 | `knowledge/components.py` | `ComponentKnowledge`: load and check `knowledge/components/` (`index.json`, `flows/*.json`; a file holding an e-mail address, URL or Id-like value is refused), strip the development evidence, resolve a typed flow name (API name, label, title, alias, did-you-means; an excluded or not-built flow named exactly comes first), the search card of each flow, the value sources of a written field (new, stage 4) |
| 1 | `knowledge/resolver.py`, `knowledge/terms.py` | The typo-tolerant term resolver (standard library only) and the knowledge and word-list part of its vocabulary; `knowledge_resolver` builds the knowledge resolver ([resolver.md](resolver.md)) |
| 2 | `engine/catalog.py` | Knowledge files joined with the warehouse schema: queryable objects, field kinds and meanings, lookup roles, sensitivity, value index, the entity index of the value spots (`entities`, `entity_key`; [value_spots.md](value_spots.md)), the guard policy, `RecordError` (`okrec/catalog.py`) |
| 2 | `engine/query_builder.py` | Pure SQL builders: fragments with bound params, scopes, filters, periods, child conditions (`okrec/query.py`) |
| 2 | `engine/sql_guard.py` | `check_sql`: one SELECT, allowlisted tables, no `*`, no table functions, no blocked columns (`okrec/guard.py`) |
| 2 | `engine/periods.py`, `engine/values.py` | Time windows in words to date ranges; the model's value to the stored spelling |
| 2 | `engine/warehouse.py`, `engine/snapshot.py`, `engine/types.py` | Read-only DuckDB connection; the local snapshot (one `docker cp` of the read copy, path checks); shared types (`RecordsStatus`, `QueryEvidence`, `Statement`, `SnapshotInfo`, `RecordsUnavailable`) |
| 2 | `engine/contact.py` | The phase-1 contact rule: `asks_for_contact` (does the question ask for contact details; the term resolver's edit distance for misspelt cue words) and `drop_contact_details` (the answer check that removes e-mail addresses and phone numbers, keeping the numbers `result_numbers` finds in the tool results) |
| 2 | `engine/business_terms.py` | The business terms the owner defined (new 2026-11-16: placement): the definitions of `prompts/business_terms.md`, the question words that name each term and the stored values that only share its word; the record hints add the definition and leave those values out |
| 2 | `engine/term_contracts.py` | The business terms enforced by code (new 2026-11-24: candidates, placement, support person performance): the question words of each term and its override words (`detect`), the candidate lookup rule (`is_candidate_lookup`), the plan for one record query call (`plan`: add the Person Account filter, refuse a call that measures a term from another source with error code T1, T2 or T3, in a question that names placement, refuse with T2 a call whose period, trend or date condition is on another field than the interview's `Date_of_Interview__c` (owner's decision of 2026-11-25): every dated `Interview__c` call (outcome filter or not: the rate's denominator), a call on another object that measures placements through a lookup to `Interview__c` (`<lookup>.Date_of_Interview__c`), and a related `Interview__c` child that keeps 'Offer Received', complete the support call with its outcome split) and the offer rate per support person (`offer_rates`); its words are `prompts/term_contracts.md`. Pure functions over a view of the catalog: no duckdb, no catalog import. The support term's hint line is registered with `engine/business_terms.py` |
| 2 | `engine/vocabulary.py` | The snapshot part of the resolver's vocabulary (stored values, person names, `SnapshotPolicy`) and `build_vocabulary` |
| 3 | `tools/registry.py` | The one definition of every tool: name, description, input schema, handler. `tools_markdown()` writes [tools.md](tools.md) |
| 3 | `tools/knowledge_tools.py` | `ToolRegistry` (15 tools over one file) and `MultiToolRegistry` (3 own tools + the 15 with an `object` argument, + the 3 component tools when components are loaded) (`okeval/tools.py`) |
| 3 | `tools/component_tools.py` | `ComponentTools`: `search_components` (a components-only BM25 index of one card per flow), `get_component` (summary, then effects, logic, fields and caveats in pages), `find_usages` (from `index.json`; object labels; a value query names every writer that may set it); budgets from `LIMITS` (new, stage 4) |
| 3 | `tools/record_tools.py` | `RecordTools`: the 14 record tools, argument checks, payload cap, evidence log. Mixins: `discovery.py` (find_objects, describe_fields, get_field_values), `records.py` (count, aggregate, group, trend, compare, percentage, list, search), `people.py` (about_people), `names.py` (name index), `lookups.py` (name grounding, lookup checks), `meta.py` (field history, sync status), `value_spots.py` (the stored record values a question names and their hint section; new 2026-10-28); shared `record_base.py` (limits, budgets) (`okrec/tools.py`) |
| 3 | `tools/term_enforce.py` | `TermEnforceMixin` of `RecordTools` (new 2026-11-24): the question's terms read in `begin_question`, the plan applied in `RecordTools.call` between the argument check and the handler (the added filter runs as an ordinary filter; a refused call is an error with code T1, T2 or T3; the view reads a `date_field` as the handler does (`_date_ref`) for the placement date rule; an added filter past the join cap is left out with a note), `applied["term_filters"]` and the code offer rate after the handler, `QueryEvidence.enforced_arguments` |
| 3 | `tools/open_records.py` | Snapshot, then connection, then catalog, then (unless `SFA_RESOLVER=off`) the full term resolver through the guarded connection, then `RecordTools` (`okrec/open.py`) |
| 4 | `agent/knowledge_agent.py`, `agent/knowledge_prompts.py`, `agent/question.py` | The tool-calling knowledge agent, its prompt texts, the `Question` dataclass |
| 4 | `agent/llm.py`, `agent/mock_llm.py` | OpenAI-compatible client with retries (3 attempts, 120 s each), and the offline `MockClient` the tests and the parity harness use |
| 4 | `agent/lean.py` | The lean knowledge step (budget enforced by `LeanRegistry`, `LeanLLM`) |
| 4 | `agent/timeline.py` | The answer's timeline (latency logging, 2026-10-29): `begin`, `end`, `add`, `stage`, `run_in`; stage, model-call, tool-call and check entries with their start and seconds, written to the JSONL record as `timeline` (standard library only; neither side: both add entries) |
| 4 | `agent/record_session.py` | `RecordSession`: route, triage, the record agent, the merge entry. Parts: `checks.py` (text cleaners, number audit), `results.py`, `nudges.py`, `triage.py`, `people_answer.py`, `merge.py` (the merge call `combine` and its checks; since 2026-10-29 also `code_merge`, the lean-mode merge by code behind `SFA_MERGE`, with `code_merge_lines` and `plans_aloud` for `pipeline.answer`, new) (`okrec/session.py`) |
| 4 | `agent/tables.py` | The record rows code writes (latency plan step 3, `SFA_TABLES=code`; new 2026-10-29): the table lines (`MARKER_RE`), the rows of each result shape from `model_result` (`render`, `column_labels`), `expand` (placement, label lines between blocks and before a block that opens the answer, drop reasons, counts), the typed rule (`_typed`: every row of a block typed by the model in a form `_key_re` and `_has_value` read drops the block, and a Total line with split totals it does not give stays: `_total_left`), `put_back` (the rows of such a block come back when a later check cuts a line of the model's copy), `own_lines` (before the reply's cleaners), `strip_markers`, `gap_before_source`, `marked` / `restore` for the merge call, `marker_message`, `answer_counts`, `line_count`, `left_over`; the record side, no duckdb, never raises |
| 4 | `agent/charts.py` | The charts code draws from record results (`SFA_CHARTS=on`; new 2026-11-03; [runbook.md](runbook.md#charts-sfa_charts)): `chart_ask` (does the question ask for a chart, code only), `drawable_ask` and `note_for` (the route overrule and the record agent's note), `drop_talk` (removes what the model says about a chart), `make` (the answer path: the last chartable result, its type, title, one SVG, or a reason line), `for_tool` (MCP `chart: true`), the core `_build` both share, `_Cleaner` (every label), `_check_svg` (fixed tags and attributes), `for_jsonl`, `text_lines`, `with_chart_arg`; the record side, pure Python, no duckdb, and `make` and `for_tool` never raise. The page imports it at start-up even with the switch off, so an import holds constants only and reads only its two prompt files |
| 4 | `agent/small_talk.py` | Small talk answered by code (new 2026-11-25, P6): `small_talk` (a full match of a word table: greeting, thanks, ack, bye, presence, empty ask, help; English, Hinglish, typos, a self-introduction after a greeting) and `line` (`prompts/small_talk.md`); standard library, the prompts and `knowledge.resolver.osa_distance` only |
| 4 | `agent/display.py` | The display words (new 2026-11-25, P3): `split` (the search text and the `Display` flags, with the guards; `SessionGuards` for the stored values, names and the invariant check, which imports the engine inside its methods), `note` (`prompts/records_display_note.md`), `line` (`prompts/display_words.md`), `CHART_VERBS`; and the chart ask grammar moved from `agent/charts.py` (`fold`, `fires`, `_ASK`, `_STOP`, `_ABOUT`, `_NOT_A_CHART`, `_FILLER` ...), which charts imports back. No engine module, no charts import |
| 4 | `agent/clarify.py` | The clarifying question of a record step that got no data (new 2026-11-25, P2): `build` (lead, 2-3 choices from the terms, the question's objects, the failed calls, the last answer or examples; `prompts/records_clarify.md`) and `choice_reply` (P2c: "2", "the second one" or the choice's text read by code) |
| 4 | `agent/followup.py` | The follow-up rewrite of the ask service (new 2026-11-04): one short model call that turns a follow-up into a question that stands on its own; `rewrite` (no earlier turn: no call), the output rule, the contact rule's check and the check that the reply is bound to the typed question; `unclear_text` writes the reply to an unclear one from `prompts/followup_unclear.md`. Copy it with `prompts/followup_rewrite.md` and `prompts/followup_unclear.md`: the page imports it at start |
| 4 | `agent/term_notes.py` | The record answer's scope claims kept true to what ran (new 2026-11-24): `drop_unapplied_scope_claims` (for a question about candidates or a call with a record-type filter: a Person Account or record-type claim no applied filter backs loses its clause, never its number), `with_term_line` (the code line "Candidates counted: Person Accounts only (...)" before the Source line), `without_term_line` (the merge's Counted line reads the record answer's own words); no duckdb, never raises |
| 4 | `agent/record_prompts.py`, `agent/render.py` | Record and merge prompt texts (`render_records_system`); the RECORDS block and trace lines |
| 5 | `evaluation/questions.py`, `grading.py`, `report.py`, `score_records.py` | Question generation, grading (fact checks and an optional judge), reports, and scoring of an `ask.sh --file` run (`okeval/*`, `score_records.py`) |
| 5 | `evaluation/runner.py`, `sets.py`, `sets.yaml`, `autograde.py`, `people_grader.py`, `expected_data.py`, `__main__.py` | The evaluation runner and its automatic grading (expected SQL re-run read-only at grading time); the sets file lists the private question and expected files by path only |
| 5 | `evaluation/knowledge_eval.py` | The single-object knowledge evaluation (`python -m sf_assistant.evaluation knowledge`): questions built from one knowledge file, the knowledge agent, grades, coverage and gaps, six files per run in `reports/knowledge_eval/` ([runbook.md](runbook.md#one-knowledge-file-on-its-own)) (`run_local.py`) |
| 5 | `evaluation/timeline_report.py` | `python -m sf_assistant.evaluation timeline`: the waterfall of an answer's timeline from the page log or an `ask --file` sidecar, and medians per stage, model-call stage and tool (`--stats`); numbers and names only ([runbook.md](runbook.md#where-the-seconds-of-an-answer-went)) |
| 5 | `server/knowledge_mcp.py` | Knowledge tools of one file as a stdio MCP server (`okeval/mcp_server.py`; needs the `mcp` package) |
| 5 | `server/app.py`, `server/data.py`, `server/auth.py`, `server/audit.py`, `server/__main__.py` | The MCP server (knowledge and record tools, resources, prompts; stdio and HTTP), what it serves (knowledge copy, record tools on the read copy, reopened after a swap), its bearer tokens, its audit log, audit key and call limits |
| 6 | `pipeline.py` | `build`, `open_record_session`, `answer` -> `Outcome`, `result_row`, `run_question` (the compute part of `ask.py`; prints nothing) |
| 7 | `cli.py` | Flags, printing, `--file` runs and transcripts; builds one `Settings` |
| 8 | `__main__.py` | `python -m sf_assistant ask ...` |
| web | `web/server.py`, `web/pipeline.py`, `web/codes.py`, `web/static/` | The LAN page (FastAPI): sign-in, queue, rate limit, log, security headers; pipeline wrapper with read-copy reopen; access codes; the page itself (no CDN) |
| web | `web/ask_mcp.py` | The ask service: the one MCP tool `ask_salesforce`, the one worker, the status rows, its log ([above](#the-ask-service)) |

Outside `src/`: `prompts/` (104 files), `knowledge/objects/` (91 knowledge files plus one raw file, git-ignored,
built by `kb_build/`), `knowledge/components/` (174 flow files and `index.json`, git-ignored, built by
`kb_build/flows/`), `kb_build/` (not in this repository), `scripts/` (`gen_tools_doc.py`, `gen_prompts_doc.py`),
`tests/`, and `.private/` (git-ignored data, see [data_and_privacy.md](data_and_privacy.md)). The package only runs
from this folder (editable install), or with `SFA_HOME` set to it. Without either, the first prompt load stops with
a `PromptError` that says so.

## Tools and prompts

- **Tools** ([tools.md](tools.md)): knowledge, one object (15); knowledge, several objects (18, and 21 with the 3
  flow component tools when `knowledge/components/` is loaded); records (14). The lean step offers 5 of the knowledge
  tools, never a component tool. Each registry gives `list_tools()` (MCP `tools/list` shape),
  `openai_tools()` and `call(name, arguments)`, which never raises and returns
  `{"ok", "result", "error", "chunk_ids"}`. `tests/test_tool_registry.py` checks the lists against the notebook
  folder's and that `tools.md` is current.
- **Prompts** ([prompts.md](prompts.md)): 96 files. 52 are prompt constants, 16 are lines code writes (nudge
  parts, FIELD CHECK, lookup footer, Source line, other-people line), and 28 came later (the spelling help, Group B,
  the evaluation fixes, the components note `knowledge_components_note`, appended to the knowledge agent's
  system prompt only when the component tools are loaded, the value spots' hint section and NO_RECORDS
  pushback, the merge by code's label line and its counted line `code_merge_counted`, and the 4 texts of the code-built rows: `records_tables_note` and
  `combine_tables_note`, appended to the record agent's and the merge call's system prompts with `SFA_TABLES=code`
  only, `records_tables_marker_message` and the words of the rows `records_table_words`, and the 2 texts of the
  charts: `records_chart_note`, appended to the record agent's system prompt on a chart question with `SFA_CHARTS=on`
  only, and the words of a chart `records_chart_words`, and the ask service's `followup_rewrite`). The modules bind each text
  at import to its
  old Python name. `tests/test_prompt_files.py` checks that each text is byte for byte the notebook folder's;
  `tests/test_prompts_doc.py` checks that `prompts.md` is current.

## Configuration

One `Settings` object (`config.load_settings`) holds what a run reads: `model` (`Config`), `records`
(`RecordsConfig`, or None with records off), `knowledge_dir`, `components_dir` (the flow components; a folder
without `index.json`: no component tools), `questions_dir`, `transcript_dirs` and `limits`. The
cli builds it once from its flags. `home`, `private_dir`, `prompts_dir` and the 14 limits that modules bind at import
(`config.IMPORT_BOUND_LIMITS`) are fixed per process: a `Settings` that changes one raises `ValueError`. The
other limits are the defaults of `Config` / `RecordsConfig`; an environment variable, a flag or an override still
wins. Each limit lives in one module, and every reader reads it there at call time (`tools.record_base`,
`agent.merge`, `agent.lean`, ...). The names per question are one limit too since the review: `tools.record_base.MAX_PEOPLE`,
read by the record tools, the triage and `RecordSession` (the notebook kept a second copy in `okrec/session.py`).
The web page's own settings class is `WebSettings` (`web/server.py`).

| Environment variable | Default | Sets |
|---|---|---|
| `OKEVAL_BASE_URL` | `http://localhost:8000/v1` | OpenAI-compatible endpoint; records need one on this machine |
| `OKEVAL_API_KEY` | `EMPTY` | API key (never printed) |
| `OKEVAL_MODEL` | `Qwen/Qwen3.6-35B-A3B-NVFP4` | model of every call |
| `OKEVAL_JUDGE_MODEL` | same as `OKEVAL_MODEL` | judge model of the evaluation |
| `OKEVAL_WAREHOUSE` | `.private/okeval_run/warehouse.duckdb` | local snapshot (`--warehouse`) |
| `OKEVAL_WAREHOUSE_CONTAINER` | `sf-local-ai-orchestrator-1` | container of `--refresh-warehouse` |
| `OKEVAL_RECORDS_TZ` | the org's time zone, else UTC | business time zone (`--tz`) |
| `OKEVAL_RECORDS_TODAY` | today | pins today, YYYY-MM-DD (`--today`) |
| `OKEVAL_RECORDS_TIMEOUT_S` | `10.0` | one SQL statement, 1-120 s |
| `OKEVAL_RECORDS_MAX_STEPS` | `12` | record agent tool rounds, 2-30 |
| `OKEVAL_RECORDS_MAX_AGE_H` | `24.0` | snapshot age warning |
| `SFA_HOME`, `SFA_KNOWLEDGE_DIR`, `SFA_PROMPTS_DIR`, `SFA_QUESTIONS_DIR` | the sf_assistant folder, `knowledge/objects`, `prompts`, `.private/questions` | folders (read once, at import); `SFA_QUESTIONS_DIR` is where the evaluation reads the `questions/...` set files and `score_records --golden` its default |
| `SFA_COMPONENTS_DIR` | `knowledge/components` | the flow components (read once, at import); a folder without `index.json`: no component tools. The MCP server also takes `--components-dir` |
| `SFA_RESOLVER` | `on` | the spelling help (term resolver): `off` or `on` (unset or empty: on); on, `pipeline.build` builds the knowledge resolver and `open_records` the full one ([resolver_integration.md](resolver_integration.md)) |
| `SFA_LEXICON` | the system lists `/usr/share/dict/american-english` and `british-english` | English word lists of the spelling help, paths separated by `:`; with none readable the spelling help stays off |
| `SFA_MERGE` | `code` | the merge of a lean answer without a FIELD CHECK line: `code` (unset or empty too: the relevant field-meaning bullets added by code, no model call) or `model` (the merge call, as before 2026-10-29); another value: code, and the JSONL says `merge_switch`. Read at each answer (`config.merge_settings`); `web/compose.yaml` does not pass it to the containers ([runbook.md](runbook.md#how-answers-are-merged-sfa_merge)) |
| `SFA_TABLES` | `model` (`config.TABLES_DEFAULT`, until the owner's go-live) | who writes a record answer's rows: `model` (the record agent types them, as before 2026-10-29) or `code` (the record agent writes a table line per result and code writes that result's rows from what the model saw); another value: the default, and the JSONL says `tables_switch`. Read at each answer (`config.tables_settings`); `web/compose.yaml` does not pass it to the containers ([runbook.md](runbook.md#how-record-rows-are-written-sfa_tables)) |
| `SFA_CHARTS` | `on` (`config.CHARTS_DEFAULT`, since the owner's go-live of 2026-11-04) | whether a question that asks for a chart gets one: `off` (nothing changes) or `on` (code draws it from the record results; MCP: `chart: true` on three tools); another value: off, and the JSONL says `charts_switch`. Read at each answer (`config.charts_settings`), and by the MCP server once at start; `web/compose.yaml` does not pass it to the containers ([runbook.md](runbook.md#charts-sfa_charts)) |

Each `OKEVAL_*` name also reads its `SFA_*` alias (`SFA_BASE_URL`, `SFA_WAREHOUSE`, ...) when the `OKEVAL_*` name
is unset or blank, so a run without `SFA_*` variables behaves exactly as before. `SFA_WAREHOUSE` is also the page's
own variable, with the same meaning (the snapshot file): `web/compose.yaml` sets it to the read copy, and the page
passes it explicitly. The page's own variables (`SFA_BIND`, `SFA_PORT`, `SFA_TIMEOUT_S`, `SFA_RATE_PER_HOUR`, ...) are
in [runbook.md](runbook.md#page-settings). The MCP server reads:

| Variable | Default | Sets |
|---|---|---|
| `SFA_MCP_BIND` | `127.0.0.1` | the only address the HTTP transport listens on (`run.sh up-mcp` warns when it is not one of this machine's) |
| `SFA_MCP_PORT` | `8700` | port (`/mcp`, `/healthz`) |
| `SFA_MCP_ALLOWED_HOSTS` | (none) | more `Host` names, comma separated |
| `SFA_MCP_RATE_PER_MIN` | `30` | tool calls per client per minute; resource reads and prompt gets: 4 times that; `0` = no limit |
| `SFA_MCP_LOG` | `.private/mcp_log.jsonl` | the audit log |
| `SFA_MCP_TOKEN_FILE` | `.private/mcp_token.json` | the token hashes |
| `SFA_MCP_AUDIT_KEY` | `.private/mcp_audit_key` | the HMAC key of the audit log's argument hashes (created when missing and the folder is writable) |

The ask service has no bind and no port. Its limits are constants in `web/ask_mcp.py`, not settings: 120 s in line, 240 s
of running, 1,000 characters per question, 12 messages of 2,000 characters (the follow-up rewrite reads the last 4).
Its socket is the constant `/run/sf-assistant-ask/ask.sock` (`SFA_ASK_SOCKET` moves it only for a run outside a
container, as the tests do; the compose file leaves it unset). Its token file is `.private/ask_token.json`; its log is
one file per business day in `.private/ask_logs/` (`web/ask_log.py`, kept `SFA_ASK_LOG_DAYS` days, 90).
`web/compose.ask.yaml` pins `OKEVAL_BASE_URL`, `OKEVAL_MODEL` and `OKEVAL_API_KEY`, as the page's service does, and the
service refuses to start with a proxy set in the environment.

**Contract v2 (Salesforce conversation memory, design of 2026-11-25).** The tool `ask_salesforce` accepts v1 and v2
calls. A v2 call adds `contract: 2`, `state` (the query state the service returned with the answer this question
replies to, at most 32 KB, opaque to the caller), `gap` (0-6), `summary` (the chat's Salesforce summary, at most 8,000
characters, data only) and `access_profile` (reserved, null today). An unknown key still gets the old service's fixed
text ("the arguments are question, recent_turns and request_id"), which the platform's bridge reads to fall back to v1;
`contract` must be 1 or 2; a `state` of another shape or version is not used and is no fault of the call. A v2 reply
holds the six result keys and four more: `contract: 2`, `state` (the next query state, or null), `plan_line` (code
written, at most 600 characters) and `reason` (a fixed code: `records`, `knowledge`, `new`, `patch`, `shortcut`,
`about_answer`, `clarify`, `small_talk`, `code_line`, `unclear_rewrite`, `refused`, `failed`, `no_answer`, `busy`,
`timeout`). No new status: a clarifying question is `answered`, kind `none`, reason `clarify`, with the choices in
`state.pending`; its text goes out as the resolver wrote it. `recent_turns`, `state` and `summary` never reach the log;
a v2 call's log line gets `memory`, fixed codes and numbers only.

The switches (`src/sf_assistant/config.py`, `MemoryConfig`; read once at the service's start, so a change needs
`./run.sh restart-ask`):

| Variable | Default | Sets |
|---|---|---|
| `SFA_MEMORY` | `shadow` | `off`: every call is v1 (no state). `shadow`: answers byte for byte as `off`; a v2 call also gets the next state built from what ran, and the resolver's code steps are logged (no model call). `on`: a v2 call goes through the follow-up resolver (`agent/resolver.py`); a patch or a question about the last answer runs the stored plan as seeded calls through the record tools, with the business terms rebuilt from the person's typed words (`agent/query_state.py`), and its "Read as:" line is the plan line. Any other value: off, with a start-up line |
| `SFA_MEMORY_SHORTCUTS` | `on` | the resolver's code shortcuts (visualize it, top N, same for a period, by month), with `on` only |
| `SFA_MEMORY_PATCH_KNOWLEDGE` | `off` | the knowledge step on a patched follow-up (off: the record agent alone) |
| `SFA_MEMORY_MAX_CALLS` | `3` | the most record calls a stored plan holds (1-3); an answer with more stores no plan |
| `SFA_MEMORY_MAX_AGE` | `2` | the most answers after its own a plan may still be patched after, with a confirm (0-6) |
| `SFA_MEMORY_TOKENS` | `12000` | the resolver's prompt cap; a value below its smallest prompt with a full plan and a question (`memory_tokens_floor()`, about 5,400) is refused |
| `SFA_ASK_LOG_DAYS` | `90` | days the daily log files are kept (`web/ask_log.py`) |

The page (`web/pipeline.py`) has no store: with `SFA_MEMORY=on` its follow-ups go through the resolver with no state;
otherwise it keeps the follow-up rewrite.

What the service assumes of its caller and its switches:

- **The shadow rewrite reads the last 4 of the turns sent** (`_rewrite_turns`, C31). Shadow answers byte for byte as
  `off` for the same call, and as a v1 bridge's call only when the last 4 turns a v2 bridge sends are the ones a v1
  bridge sends (the same filter, a user turn without an answer of its own kept). A bridge that sends whole
  question-and-answer pairs only gives the rewrite other turns when the path holds an unanswered question.
- **The memory switches are read from the ask container's environment, once at its start.** `web/compose.ask.yaml`
  passes the six `SFA_MEMORY*` names as `"${NAME:-}"`, from the shell that runs `./run.sh` or from the git-ignored
  `web/.env` (stage-2 review M1, fixed in stage 3). `tests/test_ask_mcp.py` pins the list and the service's keys, so
  no `env_file:` can hand the container the rest of `web/.env`. Unset or empty is the code default, so a start with
  none of them set runs `shadow`, which answers as `off`. `./run.sh restart-ask` takes the values of that moment, so a
  value typed only in front of one command is gone at the next `restart-ask`; one that must stay belongs in `web/.env`
  ([runbook.md](runbook.md#the-memory-switches), whose steps check the shell and the start-up line). The page's
  `web/compose.yaml` passes none of them.
- **Seeded calls run side calls first and the main call last**, so the chart (drawn from the last chartable result)
  and the next plan's main call stay the plan's main call after an operation that added a side call; a seeded main
  call stays the main call unless the record agent added a chartable call of its own.
- **The log** of an on-mode call that the resolver could not read holds its codes and the reply's length, never the
  reply.

## Tests and parity

- `tests/`: the pipeline tests ported from the notebook folder run on a fake warehouse (`warehouse_fixture.py`) and
  `MockClient`, with no model and no real data. Live tests on the local snapshot are opt-in
  (`OKEVAL_RECORDS_LIVE=1`). `test_web.py` uses a fake pipeline. A checkout without the git-ignored data runs too;
  the tests that need it skip and say which file is missing.
- The parity checks against the notebook folder read real data and stay out of this repository.

Commands are in [runbook.md](runbook.md#tests).
