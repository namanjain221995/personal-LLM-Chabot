---
id: records_no_data_line
version: 1
name: RECORDS_NO_DATA_LINE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/session.py with_no_data_line (inline text until stage 2)"
placeholders: "<<FIELDS>>"
---
The data does not record this: <<FIELDS>> is blank on every record in the warehouse, so a 0 through it is no answer.
