---
id: records_sparse_zero_message
version: 1
name: RECORDS_SPARSE_ZERO_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_SPARSE_ZERO_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<OTHERS>> <<ROLE>> <<WORD>>"
---
Your answer leads with 0 through <<FIELD>> (<<ROLE>>). Its role fits the question's word '<<WORD>>', but it is filled on almost no <<OBJECT>> records, so its 0 says little about this person. The person has records on <<OBJECT>> through <<OTHERS>> (these counts overlap: never add them). Lead with what the data holds: that <<FIELD>> is almost empty, then each of those counts with its lookup and its role as documented (for 'role not documented' or 'undocumented' say so, and never say whether it is or is not the asked role). Do not give the 0 as the answer for the person.
