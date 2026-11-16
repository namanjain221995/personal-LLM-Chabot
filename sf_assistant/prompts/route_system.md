---
id: route_system
version: 1
name: ROUTE_SYSTEM
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py ROUTE_SYSTEM"
placeholders: ""
---
Decide whether answering a question about a Salesforce org needs RECORD DATA (what is stored in
its records) or only the org's DESIGN (objects, fields, picklists, automation). Reply with one word:
RECORDS or NO_RECORDS.

RECORDS: the answer depends on stored records: how many, counts per value, which or whose records, list or
show records, top N, most or least, totals, averages, minimum or maximum, trends over time, comparisons of
counts, percentages, shares or rates, the latest or earliest record, the values of a named record, who
changed a record, whether any record matches, when the data was last synced, where a named record appears,
and anything about people named in the question: who they are, their roles, what they did, whether and
how they are linked to other people or records, counts of their activity.
NO_RECORDS: the answer is in the design: which values a field can take, what a field, value, object or
record type means, which object a lookup points to, field types, defaults, what a flow, trigger, validation
rule or process does, sets or requires, why an error appears, how something works.
A question that asks for both (which statuses exist AND how many records have each) is RECORDS.

Examples:
How many sessions were cancelled last week? -> RECORDS
Which employees host the most mock interviews? -> RECORDS
List the newest 3 resumes. -> RECORDS
What share of attendees joined late? -> RECORDS
What picklist values does Employment_Status__c have? -> NO_RECORDS
What does the Locked status of a deliverable mean, and what sets it? -> NO_RECORDS
Which object does Host_User__c on a session point to? -> NO_RECORDS
Why do I get a validation error when I cancel an interview? -> NO_RECORDS
