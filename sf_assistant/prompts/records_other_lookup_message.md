---
id: records_other_lookup_message
version: 1
name: RECORDS_OTHER_LOOKUP_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_OTHER_LOOKUP_MESSAGE"
placeholders: "<<FIELD>> <<FITS>> <<OBJECT>> <<OTHERS>> <<ROLE>>"
---
Your count used <<FIELD>> (<<ROLE>>) and found 0, but the same person has records through other lookups of <<OBJECT>> whose role may fit the question better: <<OTHERS>>.<<FITS>> These counts overlap (one record can hold the person in several lookups): never add them. Decide which role the question asks for. If another lookup fits, query it with the same grouping and answer from it, naming the lookup; if <<FIELD>> is the right role, answer 0 for that role and give the other lookups' counts with their roles. Never say 'none' for the person in general.
