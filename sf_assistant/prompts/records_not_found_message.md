---
id: records_not_found_message
version: 1
name: RECORDS_NOT_FOUND_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_NOT_FOUND_MESSAGE"
placeholders: ""
---
No record query succeeded: the name, field or object you asked for was not found (read the error). Do not give a count, not even 0. Use one of the names the error lists exactly and run the query again, or answer that no such person, field or object is recorded, without any count.
