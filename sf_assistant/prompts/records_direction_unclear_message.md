---
id: records_direction_unclear_message
version: 1
name: RECORDS_DIRECTION_UNCLEAR_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 10 (docs/people.md): the record agent's draft check when the question's word order does not tell which direction of a self-lookup it asks for and the draft names no lookup"
placeholders: "<<COUNTED>> <<FIELD>> <<FROM_SOURCE>> <<FROM_TARGET>> <<LABEL>> <<NAME>> <<OBJECT>> <<VALUE>>"
---
The question can read either direction of `<<OBJECT>>.<<FIELD>>` (<<LABEL>>): <<NAME>>'s own value ("<<FROM_SOURCE>>": <<VALUE>>), or the other <<OBJECT>> records whose `<<FIELD>>` is <<NAME>> ("<<FROM_TARGET>>").<<COUNTED>> Say which direction your answer gives and name `<<FIELD>>`, or give both.
