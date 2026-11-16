---
id: records_table_words
version: 1
name: RECORDS_TABLE_WORDS
module: sf_assistant.agent.tables
source: "new in the code-built tables of 2026-10-29 (latency plan step 3, O1; SFA_TABLES=code only): the words code writes into the rows it builds from a record result"
placeholders: "<<SHOWN>> <<TOTAL>> <<UNIT>> <<LABEL>>"
fill: "one line per word as '<key>: <text>', read into a dict by agent.tables (bound as this raw text); shown and not_all are filled with prompts.fill, UNIT with the records, groups or periods word; partial ends a period the result marks partial; by (the label line of a lines block right after another block) is filled with LABEL, a field label or a grain, and compared is that line for segments"
---
total: Total
no_value: (no value)
shown: Showing <<SHOWN>> of <<TOTAL>> <<UNIT>>.
not_all: Not all <<UNIT>> are shown.
records: records
groups: groups
periods: periods
partial: (partial)
by: By <<LABEL>>:
compared: Compared:
