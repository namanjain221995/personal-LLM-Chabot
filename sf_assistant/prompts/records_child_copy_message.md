---
id: records_child_copy_message
version: 1
name: RECORDS_CHILD_COPY_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 9 (docs/people.md, review fixes 2026-10-21): the record agent's draft check when it gives a count through a lookup that copies a lookup of a child object, without the source"
placeholders: "<<FIELD>> <<FILLED>> <<HELD>> <<OBJECT>> <<SOURCE>> <<SOURCE_FILLED>> <<VIA>>"
---
Your answer counts `<<OBJECT>>.<<FIELD>>`, a partial copy of `<<SOURCE>>`: it is filled on <<FILLED>> <<OBJECT>> records, while <<SOURCE_FILLED>> have a row with `<<SOURCE>>` set.<<HELD>> The source is the record of the role: count `<<SOURCE>>` (count_distinct `<<VIA>>` for different <<OBJECT>> records), or give both counts and say which lookup each one uses.
