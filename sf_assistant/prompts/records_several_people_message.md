---
id: records_several_people_message
version: 1
name: RECORDS_SEVERAL_PEOPLE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_SEVERAL_PEOPLE_MESSAGE"
placeholders: "<<NAMES>> <<USED>> <<WORD>>"
---
The question names '<<WORD>>', which is only a first name, and several people have it: <<NAMES>>. Your answer covers only <<USED>>. Answer for each of them (one query: filter the lookup with op in and these full names, group_by that lookup), or say that '<<WORD>>' fits several people, list them, and say which one you answered for.
