---
id: records_direction_message
version: 1
name: RECORDS_DIRECTION_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 10 (docs/people.md): the record agent's draft check when the question asks for the records that point to the person through a self-lookup and no list of them ran"
placeholders: "<<COUNTED>> <<FIELD>> <<FROM_SOURCE>> <<FROM_TARGET>> <<LABEL>> <<NAME>> <<OBJECT>> <<VALUE>>"
---
The question asks for the other <<OBJECT>> records whose `<<FIELD>>` (<<LABEL>>) is <<NAME>>: "<<FROM_TARGET>>". <<NAME>>'s own `<<FIELD>>` (<<VALUE>>) is the other direction ("<<FROM_SOURCE>>") and does not answer it.<<COUNTED>> Run list_records on <<OBJECT>> with {"field": "<<FIELD>>", "op": "eq", "value": "<<NAME>>"} and answer from its rows, naming `<<FIELD>>`.
