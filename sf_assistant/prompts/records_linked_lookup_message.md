---
id: records_linked_lookup_message
version: 1
name: RECORDS_LINKED_LOOKUP_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_LINKED_LOOKUP_MESSAGE"
placeholders: "<<FIELD>> <<OBJECT>> <<OTHERS>> <<ROLE>> <<VIA>>"
---
Your count used <<FIELD>> (<<ROLE>>) and found 0. The name matched a record that is linked to a person through <<VIA>>, and that person has records on <<OBJECT>> through <<OTHERS>>. These counts overlap (one record can hold the person in several lookups): never add them. Unless the question asks about the linked record itself (for example "as a candidate"), query that lookup with the same grouping and lead with its count, naming the lookup; give the 0 only as the count through <<FIELD>>, never as the answer.
