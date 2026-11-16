---
id: records_no_data_message
version: 1
name: RECORDS_NO_DATA_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_NO_DATA_MESSAGE"
placeholders: "<<FIELDS>>"
---
<<FIELDS>> is blank on every record in the warehouse, so no record can match it: the data does not record this, and a 0 through it is no answer. Start the answer with that ("The data does not record ...: <<FIELDS>> is blank on every record"), never with 0 or none. Counts through other lookups may follow, each with its lookup and role (never added).
