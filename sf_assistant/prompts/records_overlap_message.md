---
id: records_overlap_message
version: 1
name: RECORDS_OVERLAP_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_OVERLAP_MESSAGE"
placeholders: "<<CLAUSE>>"
---
Your answer relates counts of different lookups to each other: "<<CLAUSE>>". A lookup_check or find_references count covers every record that meets the query's other conditions (find_references: all records), not a part of your count, and no result says how the records of two lookups overlap; a difference of two counts is no count either. Rewrite that part without 'of these', 'on these records', 'include', 'additional', 'more', 'plus', sums or differences: give each count on its own with its lookup and role (the any_role count for 'in any role', written as 'in any role').
