---
id: records_people_require_tools_message
version: 1
name: RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_PEOPLE_REQUIRE_TOOLS_MESSAGE"
placeholders: "<<PEOPLE>>"
---
You answered without calling a record tool. The question names <<PEOPLE>>, and the records hold people with these names (or must be checked for them): call about_people with these names, or the query tool for a count of their records in one role, and answer only from the results.
