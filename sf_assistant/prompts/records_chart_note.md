---
id: records_chart_note
version: 1
name: RECORDS_CHART_NOTE
module: sf_assistant.agent.charts
source: "new in the charts of 2026-11-03 (SFA_CHARTS=on and a question that asks for a chart, only): appended to the record agent's system prompt in RecordSession.ask, never inside render_records_system (the MCP template answer_from_records)"
placeholders: ""
---


This question asks for a chart. Code draws it after you answer, from your tool results. So fetch the numbers with group_records (by a field) or trend_records (over time; when no period is named use 'last 12 months' and say so), not list_records; if the question also asks to list records, list them too. If the question does not say what to count, reply NO_RECORDS. Answer in words as usual. Never type a chart, bars or code, and never say whether a chart can be drawn.
