---
id: records_people_tool_message
version: 1
name: RECORDS_PEOPLE_TOOL_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_PEOPLE_TOOL_MESSAGE"
placeholders: "<<MISSING>> <<NAMES>> <<NAMES_JSON>>"
---
The question names <<NAMES>>, and no result covers <<MISSING>>. Call about_people with names <<NAMES_JSON>>: it gives who each person is, where they are referenced and every stored record that links them. Then answer from it, keeping the counts you already have, each with its lookup.
