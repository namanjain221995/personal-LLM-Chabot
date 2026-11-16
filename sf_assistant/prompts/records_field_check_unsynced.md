---
id: records_field_check_unsynced
version: 1
name: RECORDS_FIELD_CHECK_UNSYNCED
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/session.py field_checks (inline text until stage 2)"
placeholders: "<<FIELD>> <<OBJECT>> <<PROXY>> <<QUERY_ID>> <<ROLE>> <<WORD>>"
---
The question's word '<<WORD>>' fits <<OBJECT>>.<<FIELD>><<ROLE>>, which is documented but not synced to the warehouse: the data does not record it. Record counts on <<OBJECT>> go through <<PROXY>> instead (a proxy, RECORD FACTS query <<QUERY_ID>>).
