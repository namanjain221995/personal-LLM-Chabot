---
id: records_tools_forced_message
version: 1
name: RECORDS_TOOLS_FORCED_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in the prerequisites of 2026-11-25 (P2 records retry): sent with tool_choice required for the one forced round of a records-routed question whose draft ran no record tool (RecordSession.ask)"
placeholders: ""
---
No record tool has run yet, so there are no results to answer from. Call the record tool that answers the question now (count_records, group_records, trend_records, compare_records, aggregate_records, percentage_of_records or list_records; describe_fields first only if you do not know the field). Then answer only from its result; reply NO_RECORDS only if the result shows that the question needs no record data.
