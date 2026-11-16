---
id: records_unsupported_message
version: 1
name: RECORDS_UNSUPPORTED_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_UNSUPPORTED_MESSAGE"
placeholders: "<<NUMBERS>>"
---
These numbers in your answer are in no tool result: <<NUMBERS>>. Do no arithmetic and do not estimate. Call the query tool that returns the number you need (aggregate_records for an average or total, compare_records for a difference, percentage_of_records for a share), or answer without those numbers. Do not mention those numbers, the tools or this check in the answer.
