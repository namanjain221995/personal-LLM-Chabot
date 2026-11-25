---
id: records_display_note
version: 2
name: RECORDS_DISPLAY_NOTE
module: sf_assistant.agent.display
source: "new in the prerequisites of 2026-11-25 (P3 display words): how to lay out the record answer when the question said so; code removed those words from the question; appended to the record agent's system prompt after the chart note in RecordSession.ask, never inside render_records_system (the MCP template answer_from_records); v2 (review of 2026-11-25): the breakdown line splits by the field the question names first"
placeholders: ""
fill: "one line per key as '<key>: <text>', read into a dict by agent.display (bound as this raw text); the note is a blank line, the lead line and the line of each flag the question set"
---
lead: How to lay out the answer (code read this from the question and removed the words from it; they name no field or object):
table: Give the rows as a Markdown table, one row per record or group.
detail: Give the main fields of each record, not only counts.
summary: Lead with the totals, then at most 4 short bullets.
breakdown: Split the result by the field the question names, else by its main field (such as its status), and give the count of each part.
file: No file can be made here: give the rows in the answer.
