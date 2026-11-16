---
id: records_direction_source_message
version: 1
name: RECORDS_DIRECTION_SOURCE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 10 (docs/people.md): the record agent's draft check when the question asks for the person's own value of a self-lookup and the draft does not give it"
placeholders: "<<FIELD>> <<FROM_SOURCE>> <<FROM_TARGET>> <<LABEL>> <<NAME>> <<OBJECT>> <<VALUE>>"
---
The question asks for <<NAME>>'s own `<<FIELD>>` (<<LABEL>>: "<<FROM_SOURCE>>"), which is <<VALUE>> on <<NAME>>'s <<OBJECT>> record. The records whose `<<FIELD>>` is <<NAME>> are the other direction ("<<FROM_TARGET>>"). Answer with <<VALUE>> and name `<<FIELD>>`.
