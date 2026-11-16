---
id: records_relation_lookup_unclear_message
version: 1
name: RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in the evaluation fixes of 2026-10-22 (fix 5, docs/people.md; with Group B item 10): the record agent's draft check when a word of the question is in the knowledge wording of a self-lookup other than the one the answer used, and the word order does not tell which direction it asks for"
placeholders: "<<COUNTED>> <<FIELD>> <<FROM_SOURCE>> <<FROM_TARGET>> <<LABEL>> <<NAME>> <<OBJECT>> <<USED>> <<WORD>>"
---
The question's word '<<WORD>>' is in the knowledge wording of `<<OBJECT>>.<<FIELD>>` (<<LABEL>>: "<<FROM_SOURCE>>"; from the other side, "<<FROM_TARGET>>"), not in that of <<USED>>, which your answer used. The question can read either direction: <<NAME>>'s own `<<FIELD>>` ("<<FROM_SOURCE>>"), or the <<OBJECT>> records whose `<<FIELD>>` is <<NAME>> ("<<FROM_TARGET>>").<<COUNTED>> Say which direction your answer gives and name `<<FIELD>>`, or give both.
