---
id: records_breakdown_message
version: 1
name: RECORDS_BREAKDOWN_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_BREAKDOWN_MESSAGE"
placeholders: "<<ITEMS>>"
---
These breakdown counts in your answer are not in the results: <<ITEMS>>. Copy every group and its count exactly as the grouped result gives them (groups, by, split_totals, breakdown); a value that is not among the result's groups has no records under the query's conditions (leave it out or give 0). Do not estimate or carry counts over from describe_fields.
