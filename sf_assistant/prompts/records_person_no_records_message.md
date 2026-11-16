---
id: records_person_no_records_message
version: 1
name: RECORDS_PERSON_NO_RECORDS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_PERSON_NO_RECORDS_MESSAGE"
placeholders: "<<PEOPLE>>"
---
The question names <<PEOPLE>>: code matched these names to stored people (or found no stored person with a name the question gives), so it needs record data: do not reply NO_RECORDS. Call about_people with these names (or the query tool for a count of their records in one role) and answer from the results. If the lookup the question's words fit is not in the warehouse (hints, notes), say first that the data does not record it, then give a count through the closest lookup only as a proxy, naming its lookup.
