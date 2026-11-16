---
id: records_other_lookup_fits_message
version: 1
name: RECORDS_OTHER_LOOKUP_FITS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_OTHER_LOOKUP_FITS_MESSAGE"
placeholders: "<<COUNT>> <<FIELD>> <<OBJECT>> <<OTHERS>> <<ROLE>> <<WORD>>"
---
Your count used <<FIELD>> (<<ROLE>>): <<COUNT>>. The question's word '<<WORD>>' fits another lookup of <<OBJECT>>, which has records under the same conditions: <<OTHERS>>. These counts overlap (one record can hold the person in several lookups): never add them. If that role is what the question asks for, query that lookup with the same grouping and answer from it, naming the lookup; if <<FIELD>> is the right role, keep your count and name <<FIELD>> and its role.
