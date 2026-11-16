---
id: records_same_name_employee_message
version: 1
name: RECORDS_SAME_NAME_EMPLOYEE_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_SAME_NAME_EMPLOYEE_MESSAGE"
placeholders: "<<COUNT>> <<FIELD>> <<OBJECT>> <<OTHERS>> <<OTHER_FIELD>> <<ROLE>> <<WORD>>"
---
Your count used <<FIELD>> (<<ROLE>>) on a candidate record named like the person: <<COUNT>>. The same name is also an Employee, a different record, who has records on <<OBJECT>> under the same conditions: <<OTHERS>>; the question's word '<<WORD>>' fits that role for an employee. Unless the question says candidate, the employee is meant: query <<OTHER_FIELD>> with the same grouping and answer from it, naming the lookup; then give the candidate record's count on its own, as a different record with the same name. Never add these counts.
