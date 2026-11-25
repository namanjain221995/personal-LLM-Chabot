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

## Logs and kept files

Logs are written under `.private/` (mode 600, git-ignored). The package does not rotate or delete them. The keys the
prerequisites of 2026-11-25 add to an answer's log line hold no new personal data: `display` holds the display words of
a fixed list and the words a guard kept (the question itself is already in the line), `clarify` and `record_tool_retry`
hold fixed words, object API names and counts, and `line_reason` / `small_talk` a fixed word.
