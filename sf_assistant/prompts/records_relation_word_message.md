---
id: records_relation_word_message
version: 1
name: RECORDS_RELATION_WORD_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 10 (docs/people.md): the record agent's draft check of a relation word that no lookup holding the named person carries"
placeholders: "<<CLAUSE>> <<HOLDS>> <<WORD>> <<WORD_FIELDS>>"
---
Your answer says "<<CLAUSE>>", but in the records <<HOLDS>>. '<<WORD>>' is the label or role of <<WORD_FIELDS>>, which does not hold that person here. Call each relation by the label or role of the lookup that holds it, name that lookup, and keep the people of different lookups apart.
