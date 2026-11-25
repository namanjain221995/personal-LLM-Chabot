# Data and privacy

A short public note, not a privacy notice. The full record of how a deployment handles data stays with whoever runs
it, outside this repository.

- This repository holds code, prompts, tests and docs. It holds no records, logs, access codes or tokens: `.gitignore`
  keeps them out. Test fixtures hold made-up records and text; `tests/fixtures/Internal_Interview__c.json` keeps API
  names and some picklist values of one object.
- The package answers from Salesforce records in a local, read-only copy. Those records can hold personal data. Whoever
  runs the package decides who may read them and how long anything is kept, and answers for that.
- The record tools refuse credential, government-identity and bank-detail fields and mask known secret patterns in the
  values they show. Contact fields show a value only when the question asks for it. These are safeguards among people
  already trusted with the data, not a security boundary: stored text can hold anything. Give an access code or a token
  only to someone cleared to read all of it.

## Flow components: evidence and withheld fields

The flow component files are not in this repository. They describe what flows do and hold no record. Their development
evidence is stripped when they load, so no tool returns it, and withheld fields stay withheld.

## MCP clients and record data

The MCP server makes no model call: a client sends the tool results to its own model. A client on a cloud model
therefore sends record data off this machine. Connect only clients cleared for that data. Text in records is untrusted.

## Conversation memory: the query state

With conversation memory (design of 2026-11-25; `SFA_MEMORY`, wired by the ask service's contract v2, not on yet),
the ask service returns with each answer a `state` and a one-line `plan_line`, both written by code
(`src/sf_assistant/agent/query_state.py`), and the platform stores them in its `sf_query_state` table and folds the
plan lines into the chat's Salesforce summary. These are Salesforce data of the chat. They are deleted with the chat
(and with its user) on the platform; the service keeps nothing between questions.

- **What they hold:** the record queries that ran in their model-written form (objects, fields, filters, periods and
  their dates, groupings, limits), the business-term keys and the person's own typed questions of the chain (at most
  600 characters), the display flags, and a pending clarifying question with its choices. A filter value is kept only
  when the person typed it, or it is a date, true or false, or a picklist value or record type of its field. A query
  of the record agent that holds a name, a figure or a record Id taken from a result is left out, and compare labels
  are code's words, so no result name or figure reaches the state, the plan line or the summary.
- **What they never hold:** result rows, counts, answer text, SQL, matched names, record Ids nobody typed, the
  business-term flags or the contact flag.
- The resolver's model call (on this machine only) reads the plan, the summary, the pending choices and up to 12 recent
  turns, within `SFA_MEMORY_TOKENS`; no prompt above that cap is sent.

## Logs and kept files

Logs are written under `.private/` (mode 600, git-ignored).

- **The ask service's log** (since 2026-11-25) is one file per business day (Asia/Kolkata) in `.private/ask_logs/` (mode
  700). The service deletes its own daily files once they are more than 90 days old (`SFA_ASK_LOG_DAYS`, the approved
  retention; [runbook.md](runbook.md#the-ask-log)), and nothing else. It runs no timer: a file is deleted at the
  service's first start or write after its 90 days, so a service that writes nothing keeps it longer (at least 90
  days). Its lines hold real names: the questions, the
  answers and the values of the record queries. The log has no chat id, so the lines of a chat deleted on the platform
  stay until their day's file is deleted, up to 90 days.
- The one-time split of the old single file keeps it as `.private/ask_log.legacy.jsonl` and puts lines with no readable
  time into `.private/ask_logs/ask_log-unknown-date.jsonl`. Neither is deleted by the service: they are deleted by hand
  from the day the split names, the date of the backup's newest line plus 91 days (the day its daily file goes).
- The ask service's container log holds start-up lines, class names and deletion counts, no question text, and is
  capped at 3 files of 10 MB.
- The page's log (`web_log.jsonl`) and the MCP server's log (`mcp_log.jsonl`) are not rotated or deleted yet. The
  recommended retention for them is the same 90 days; it is not built.

The keys the prerequisites of 2026-11-25 add to an answer's log line hold no new personal data: `display` holds the
display words of a fixed list and the words a guard kept (the question itself is already in the line), `clarify` and
`record_tool_retry` hold fixed words, object API names and counts, and `line_reason` / `small_talk` a fixed word.
