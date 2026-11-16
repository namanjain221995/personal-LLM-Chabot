---
id: records_footer_same_person_for
version: 1
name: RECORDS_FOOTER_SAME_PERSON_FOR
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/session.py with_lookup_footer (inline text until stage 2)"
placeholders: "<<NAMES>> <<OBJECT>> <<OTHERS>>"
---
Same person through other lookups of <<OBJECT>> for <<NAMES>> (overlapping, not to be added): <<OTHERS>>.
