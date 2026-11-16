---
id: records_not_synced_message
version: 1
name: RECORDS_NOT_SYNCED_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_NOT_SYNCED_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<PROXY>> <<ROLE>> <<WORD>>"
---
The question's word '<<WORD>>' fits <<OBJECT>>.<<FIELD>> (<<ROLE>>), which is documented but not synced to the warehouse: the data does not record it. Start the answer with that (never 0, none or 'has not' for it). A count through <<PROXY>> may follow only as a proxy: call it a proxy and name its lookup or path.
