---
id: records_field_check_lookup
version: 1
name: RECORDS_FIELD_CHECK_LOOKUP
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/session.py field_checks (inline text until stage 2)"
placeholders: "<<FIELD>> <<OBJECT>> <<OTHER_FIELD>> <<OTHER_RECORDS>> <<OTHER_ROLE>> <<QUERY_ID>> <<RECORDS>> <<ROLE>>"
---
The question's verb fits <<OBJECT>>.<<OTHER_FIELD>><<OTHER_ROLE>>, which the knowledge answer names; the record count used <<OBJECT>>.<<FIELD>><<ROLE>>: <<RECORDS>>. With the same conditions, <<OTHER_FIELD>> has <<OTHER_RECORDS>> (RECORD FACTS, query <<QUERY_ID>>).
