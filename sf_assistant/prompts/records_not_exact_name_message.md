---
id: records_not_exact_name_message
version: 1
name: RECORDS_NOT_EXACT_NAME_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_NOT_EXACT_NAME_MESSAGE"
placeholders: "<<ASKED>> <<USED>>"
---
No record is named '<<ASKED>>' (the question's spelling); your answer uses the stored name <<USED>>. Say that no record is named '<<ASKED>>' and that you used the closest stored name <<USED>>, then give the numbers.
