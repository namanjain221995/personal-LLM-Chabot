---
id: records_other_lookup_firm_message
version: 1
name: RECORDS_OTHER_LOOKUP_FIRM_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_OTHER_LOOKUP_FIRM_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<OTHERS>> <<OTHER_FIELD>> <<ROLE>> <<SPARSE>> <<WORD>>"
---
Your count used <<FIELD>> (<<ROLE>>) and found 0, but the question's word '<<WORD>>' fits another lookup of <<OBJECT>> that holds the same person: <<OTHERS>>.<<SPARSE>> The 0 of <<FIELD>> is no answer to the question: query <<OTHER_FIELD>> with the same grouping and lead with its count, naming the lookup and its role. The 0 of <<FIELD>> may follow only as a side note with its role. These counts overlap (one record can hold the person in several lookups): never add them. Never say 'none' for the person in general.
