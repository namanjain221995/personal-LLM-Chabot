---
id: records_tables_note
version: 2
name: RECORDS_TABLES_NOTE
module: sf_assistant.agent.tables
source: "new in the code-built tables of 2026-10-29 (latency plan step 3, O1; SFA_TABLES=code only): appended to the record agent's system prompt in RecordSession.ask, never inside render_records_system (the MCP template answer_from_records)"
placeholders: ""
---


Rows by code: for each list_records, group_records, trend_records or compare_records result or count_records breakdown with 2 or more rows to show, write a table line with only {{table q3}} (q3 = its query_id). Code copies its rows and any Total line there exactly: that is your copy of them, also of a breakdown the rules above say to copy.
- Never type those rows in any form, even in a redraft: no line per row, month name, range, split or pivot.
- Always start with 1-3 sentences, never a table line or list. State the numbers the question asks for, as the result gives them.
- After the table lines, add nothing the rows show: one bullet on what was counted (field and lookup API names in backticks), any caveat, the Source line.
- No table line for about_people (keep its bullets), find_references or get_field_history.
- No total for a top-N ranking unless asked. total_across_groups includes the (blank) group. Never state a describe_fields or find_objects count.

Example answer:
There are 61 gadgets.
{{table q3}}
- Counted: `Gadget__c` by `Color__c`, `Status__c` = Open, `CreatedDate` from 2031-05-01 to 2031-05-31.
