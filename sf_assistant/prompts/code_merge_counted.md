---
id: code_merge_counted
version: 1
name: CODE_MERGE_COUNTED
module: sf_assistant.agent.merge
source: "new in the lean-mode merge by code (step-2 fixes of 2026-10-30, F8): the code line that says what the answer's one counting query counted when the record answer leaves it out"
placeholders: "<<PARTS>> <<MEASURE_FIELD>> <<OBJECT>> <<TOTAL>> <<GROUP_FIELD>> <<FILTERS>> <<DATE_FIELD>> <<FROM>> <<TO>> <<ZONE>> <<VALUE>>"
fill: "one key: text line per part, read into a dict by agent.merge (bound as this raw text); each part is filled with prompts.fill"
---
counted: Counted: <<PARTS>>.
summed: Summed `<<MEASURE_FIELD>>` over <<PARTS>>.
records: `<<OBJECT>>` records
total: <<TOTAL>> `<<OBJECT>>` records
by: grouped by `<<GROUP_FIELD>>`
where: where <<FILTERS>>
period: with `<<DATE_FIELD>>` from <<FROM>> to <<TO>> (<<ZONE>>)
period_from: with `<<DATE_FIELD>>` on or after <<FROM>> (<<ZONE>>)
period_to: with `<<DATE_FIELD>>` up to <<TO>> (<<ZONE>>)
blank: the blank group (<<VALUE>>) is left out of the ranking
