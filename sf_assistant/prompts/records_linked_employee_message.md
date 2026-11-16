---
id: records_linked_employee_message
version: 1
name: RECORDS_LINKED_EMPLOYEE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_LINKED_EMPLOYEE_MESSAGE"
placeholders: "<<COUNT>> <<FIELD>> <<OBJECT>> <<OTHERS>> <<OTHER_FIELD>> <<ROLE>> <<VIA>>"
---
Your count used <<FIELD>> (<<ROLE>>): <<COUNT>>. The name matched an Account that is an Employee's own record (linked through <<VIA>>), so this person is that employee, who has records on <<OBJECT>> through <<OTHERS>>. Unless the question asks about the person as a candidate, the employee's role is meant: query <<OTHER_FIELD>> with the same grouping and lead with its count, naming the lookup and its role; then give the <<FIELD>> count as their count as a candidate. These counts overlap (one record can hold the person in several lookups): never add them.
