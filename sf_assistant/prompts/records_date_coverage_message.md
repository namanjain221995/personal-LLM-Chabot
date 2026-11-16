---
id: records_date_coverage_message
version: 1
name: RECORDS_DATE_COVERAGE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_DATE_COVERAGE_MESSAGE"
placeholders: "<<FIELD>> <<MAIN>> <<MISSING>>"
---
Your trend used <<FIELD>> as the date, but <<MISSING>> records that meet the other conditions have no <<FIELD>> (records_without_date), so they are in no bucket. Unless the question names that date, run the trend again with date_field = <<MAIN>>, the same filters and grain, and answer from it, naming the date field. If the question does mean <<FIELD>>, keep it and say that <<MISSING>> records have no <<FIELD>>.
