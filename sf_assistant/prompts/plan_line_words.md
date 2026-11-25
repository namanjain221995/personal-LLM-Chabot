---
id: plan_line_words
version: 1
name: PLAN_LINE_WORDS
module: sf_assistant.agent.query_state
source: "new in the Salesforce conversation memory of 2026-11-25 (stage 2, review point N1: moved as it was from agent/query_state.py): the words of the plan line code writes for a stored query plan (the Read-as line of a patch, the confirm questions and the plan lines the Salesforce summary folds)"
placeholders: "<<OBJ>> <<FIELD>> <<OP>> <<MEASURE>> <<GROUP>> <<GRAIN>> <<PART>> <<SEGMENTS>> <<ORDER>> <<FILTERS>> <<CHILD>> <<N>> <<PHRASE>> <<RANGE>> <<FROM>> <<TO>> <<TYPE>> <<CALL>> <<SET>>"
fill: "one line per key as '<key>: <text>', read into a dict by agent.query_state and filled with prompts.fill (one pass); the plan line holds no figure of a result"
---
count: number of <<OBJ>>
distinct: number of different <<FIELD>> in <<OBJ>>
measure: <<OP>> of <<FIELD>> in <<OBJ>>
group: <<MEASURE>> per <<GROUP>>
split: split by <<FIELD>>
trend: <<MEASURE>> by <<GRAIN>>
by: by <<GRAIN>>
rate: share of <<OBJ>> where <<PART>>
compare: <<MEASURE>>: <<SEGMENTS>>
versus: vs
list: <<OBJ>> listed
sorted: sorted by <<FIELD>> <<ORDER>>
where: where <<FILTERS>>
and: and
any_of: at least one of <<FILTERS>>
or: or
with_child: with <<CHILD>> records
without_child: without <<CHILD>> records
candidates: candidates as Person Accounts
all_types: every account type
segment: part <<N>>
period: <<PHRASE>> (<<RANGE>>)
range: <<FROM>> to <<TO>>
since: since <<FROM>>
until: until <<TO>>
top: top <<N>>
bottom: bottom <<N>>
limit: <<N>> rows
min_value: groups of at least <<N>>
chart: as a chart
chart_type: as a <<TYPE>> chart
table: as a table
also: also <<CALL>>
set: for the <<SET>> shown before
set_rank: for number <<N>> of the <<SET>> shown before
set_top: top <<N>> <<FIELD>>
set_list: <<OBJ>> listed
records: <<OBJ>> records
op_count: number
op_count_distinct: number of different
op_sum: total
op_avg: average
op_median: median
op_min: lowest
op_max: highest
