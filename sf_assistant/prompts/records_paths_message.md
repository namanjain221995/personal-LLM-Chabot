---
id: records_paths_message
version: 1
name: RECORDS_PATHS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in Group B item 8 (docs/people.md, review fixes 2026-10-21): the record agent's draft check when it gives a person's count through the record's own lookup only while the same person reaches more of these records through their parents in the same role"
placeholders: "<<ANY>> <<FIELD>> <<NAME>> <<OBJECT>> <<PATHS>> <<RECORDS>>"
---
Your answer gives <<RECORDS>> <<OBJECT>> records with <<NAME>> in `<<FIELD>>`, but the lookup check also reaches <<NAME>> through the parents of <<OBJECT>> records in the same role: <<PATHS>>. <<ANY>> <<OBJECT>> records are in `<<FIELD>>` or in any of these paths (they overlap: never add the counts). Give <<ANY>> as the count through every path and name the paths, or say that <<RECORDS>> counts only `<<FIELD>>` and give <<ANY>> next to it.
