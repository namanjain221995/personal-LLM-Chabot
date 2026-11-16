---
id: records_shared_name_message
version: 1
name: RECORDS_SHARED_NAME_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_SHARED_NAME_MESSAGE"
placeholders: "<<FIELD>> <<LABEL>> <<N>> <<NAME>> <<RECORDS>>"
---
'<<NAME>>' is the name of <<N>> different <<LABEL>> records; through <<FIELD>> they count: <<RECORDS>>. Your answer does not say that several records share this name. Say so and give the count of each record (as '<name> (#k)'), or say which one you answered for; never present them as one person.
