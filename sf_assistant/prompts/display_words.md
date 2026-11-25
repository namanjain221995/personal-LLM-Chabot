---
id: display_words
version: 2
name: DISPLAY_WORDS_TEXT
module: sf_assistant.agent.display
source: "new in the prerequisites of 2026-11-25 (P3 display words): the lines code writes for a question that only says how to show something (no data named, or a capability ask) and for a file ask, with no model call (agent.display); v2 (review of 2026-11-25): the capability line fits the ask (a file, a dashboard, or tables only with SFA_CHARTS off) and the bare line has a tables-only form"
placeholders: ""
fill: "one line per key as '<key>: <text>', read into a dict by agent.display (bound as this raw text); no placeholders"
---
bare: Name the data to show in the same question, for example: interviews per month for the last six months as a chart.
bare_tables: Name the data to show in the same question, for example: interviews per month for the last six months as a table.
capability: Yes. I can show record results as bar, line or pie charts and as tables. Name the data in the same question, for example: bar chart of interviews by status for this year.
capability_tables: Yes. I can show record results as tables. Name the data in the same question, for example: interviews by status for this year as a table.
capability_visual: No dashboards are made here, but I can show record results as bar, line or pie charts and as tables. Name the data in the same question, for example: bar chart of interviews by status for this year.
capability_visual_tables: No dashboards are made here, but I can show record results as tables. Name the data in the same question, for example: interviews by status for this year as a table.
capability_file: No file is made here. Name the data in the same question and the answer gives its rows, for example: interviews by status for this year as a table.
file: No file is made here: the figures are in this answer.
