---
id: records_unfit_zero_message
version: 1
name: RECORDS_UNFIT_ZERO_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_UNFIT_ZERO_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<OTHERS>> <<ROLE>> <<SPARSE>> <<WORD>>"
---
Your answer leads with 0, but that 0 is the count through <<FIELD>> (<<ROLE>>), whose role does not fit the question's word '<<WORD>>': a 0 through a lookup of another role says nothing about the asked role.<<SPARSE>> The person has records on <<OBJECT>> through <<OTHERS>> (these counts overlap: never add them). Lead with what the data holds for the asked role: the lookup whose role fits '<<WORD>>' and its count, or that no lookup of <<OBJECT>> with data is documented with that role; then give the other lookups' counts, each with its lookup and role. Never give the 0 of <<FIELD>> as the answer.
