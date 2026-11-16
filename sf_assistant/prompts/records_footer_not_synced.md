---
id: records_footer_not_synced
version: 1
name: RECORDS_FOOTER_NOT_SYNCED
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/session.py with_lookup_footer (inline text until stage 2)"
placeholders: "<<FIELD>> <<OBJECT>> <<PROXY>> <<ROLE>> <<WORD>>"
---
Not in the warehouse: `<<OBJECT>>.<<FIELD>>`<<ROLE>>, which the question's word '<<WORD>>' fits, is documented but not synced, so the data does not record it; counts through <<PROXY>> are a proxy.
