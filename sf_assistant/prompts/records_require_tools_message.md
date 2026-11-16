---
id: records_require_tools_message
version: 1
name: RECORDS_REQUIRE_TOOLS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_REQUIRE_TOOLS_MESSAGE"
placeholders: ""
---
You answered without calling a record tool. If the question needs record data, call the record tools now and answer only from their results. If it does not need record data, reply exactly NO_RECORDS.
