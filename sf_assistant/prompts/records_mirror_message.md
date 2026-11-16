---
id: records_mirror_message
version: 1
name: RECORDS_MIRROR_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_MIRROR_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<SOURCE>>"
---
Your query on <<OBJECT>> used <<FIELD>>, which copies <<SOURCE>> (the copy can be blank or out of date). Run the same query with <<SOURCE>> in place of <<FIELD>> and answer from that result, naming <<SOURCE>>.
