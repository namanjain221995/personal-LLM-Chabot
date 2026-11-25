---
id: records_clarify
version: 1
name: RECORDS_CLARIFY_TEXT
module: sf_assistant.agent.clarify
source: "new in the prerequisites of 2026-11-25 (P2 clarifying question): the lines code writes when the record step of a records-routed question ran no record tool, or every call failed, after the forced retry: a lead, 2-3 numbered choices and a closing line (agent.clarify)"
placeholders: "<<OBJECTS>> <<FIELD>> <<PERIOD>> <<VERB>>"
fill: "one line per key as '<key>: <text>', read into a dict by agent.clarify (bound as this raw text); each choice is filled with prompts.fill: OBJECTS the plural label in lower case, FIELD a field label, PERIOD the question's period words with their lead word (or empty), VERB a verb_* line"
---
lead: I could not get the figures for this question. Did you mean one of these?
lead_no_object: I could not get the figures for this question. Name what to count and the period, for example:
tail: Reply with the number, or ask the question again in your own words.
count: How many <<OBJECTS>> were there<<PERIOD>>?
count_all: How many <<OBJECTS>> are there?
per_month: <<VERB>> the number of <<OBJECTS>> per month<<PERIOD>>
by_field: <<VERB>> <<OBJECTS>> by <<FIELD>><<PERIOD>>
term_support: <<VERB>> support person performance<<PERIOD>>
term_placement: <<VERB>> the placement rate<<PERIOD>>
example_1: How many interviews were there last month?
example_2: Show the number of interviews per month in the last 6 months
example_3: Show candidates by their status this year
verb_show: Show
verb_plot: Plot
verb_table: Show a table of
