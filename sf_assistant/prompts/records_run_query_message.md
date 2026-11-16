---
id: records_run_query_message
version: 1
name: RECORDS_RUN_QUERY_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_RUN_QUERY_MESSAGE"
placeholders: ""
---
Your answer states numbers but no query tool ran. The record counts in find_objects and describe_fields only help choose the object, field and value; they ignore filters and periods and hold no averages or totals. Call the query tool for this question now (count_records, aggregate_records, group_records, trend_records, compare_records, percentage_of_records or list_records), then answer only from its result. If the question names no person and only asks what a field, value or object means or how something works, it needs no record data: reply exactly NO_RECORDS.
