---
id: records_list_note
version: 1
name: RECORDS_LIST_NOTE
module: sf_assistant.agent.tables
source: "new in development A of 2026-11-27 (long record lists): appended to the record agent's system prompt in RecordSession.ask when the question asks to list records (agent.display.asks_list), whatever SFA_TABLES says, never inside render_records_system (the MCP template answer_from_records); code writes every list_records row under the answer (agent.tables.list_rows)"
placeholders: ""
---


Lists by code: this question asks for a list of records. When you answer it with list_records:
- Call list_records once, with limit 200 and only the fields the question needs (people by their names). Never call it again for more rows or the next page: code keeps every row the call returns (returned), also the rows a trimmed result does not show you (shown).
- Start with 1-3 sentences: how many records match (total_count) and what was listed. Then a line with only {{table q3}} (q3 = that result's query_id): code writes every row there. Then one bullet on what was listed (object, field and filter API names in backticks), any caveat and the Source line.
- Never type the rows yourself in any form: no line, bullet, name or value per record.

Example answer:
There are 61 gadgets with `Color__c` = Blue.
{{table q3}}
- Listed: `Gadget__c` with `Color__c` = Blue, sorted by `Name`.
