---
id: records_relation_lookup_message
version: 1
name: RECORDS_RELATION_LOOKUP_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in the evaluation fixes of 2026-10-22 (fix 5, docs/people.md; with Group B item 10): the record agent's draft check when a word of the question is in the knowledge wording (label, from_source, from_target) of a person lookup other than the one the answer used, and the question asks for the records that point to the person through it"
placeholders: "<<COUNTED>> <<FIELD>> <<FROM_SOURCE>> <<FROM_TARGET>> <<LABEL>> <<NAME>> <<OBJECT>> <<USED>> <<WORD>>"
---
The question's word '<<WORD>>' is in the knowledge wording of `<<OBJECT>>.<<FIELD>>` (<<LABEL>>: "<<FROM_SOURCE>>"; from the other side, "<<FROM_TARGET>>"), not in that of <<USED>>, which your answer used. The question asks for the <<OBJECT>> records whose `<<FIELD>>` is <<NAME>>: "<<FROM_TARGET>>".<<COUNTED>> Query <<OBJECT>> with {"field": "<<FIELD>>", "op": "eq", "value": "<<NAME>>"} (list_records for who or which they are, count_records for how many) and answer from that result, naming `<<FIELD>>`.
