---
id: records_elsewhere_message
version: 1
name: RECORDS_ELSEWHERE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_ELSEWHERE_MESSAGE"
placeholders: "<<ELSEWHERE>> <<FIELD>> <<OBJECT>> <<ROLE>>"
---
Your count on <<OBJECT>> used <<FIELD>> (<<ROLE>>) and found 0, and no lookup of <<OBJECT>> holds this person under the same conditions. The person is referenced on other objects (all records, no other conditions; these counts overlap, never add them): <<ELSEWHERE>>. If the question's noun can mean one of those objects (for example "interviews" fits both Interview__c and Internal_Interview__c), query that object with the lookup whose role fits the verb, keeping the question's grouping, and answer from it. If <<OBJECT>> is the object the question means, answer 0 for it, naming the lookup, and do not say 'none' for the person in general. Keep every count and record your answer already gives for other people or objects.
