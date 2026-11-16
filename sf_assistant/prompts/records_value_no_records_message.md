---
id: records_value_no_records_message
version: 1
name: RECORDS_VALUE_NO_RECORDS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in the value spots of 2026-10-28 (every stored record value a question names): the record agent's draft check when it replies NO_RECORDS although code matched words of the question to stored record values (a strong listed value spot of the triage)"
placeholders: "<<VALUES>>"
---
The question names stored records: <<VALUES>> (code matched these words to stored values), so it needs record data: do not reply NO_RECORDS. Look each one up with list_records filtered on the field the hints give, count its linked records, and answer from the results; when no description is stored, say so.
