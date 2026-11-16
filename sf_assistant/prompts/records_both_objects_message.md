---
id: records_both_objects_message
version: 1
name: RECORDS_BOTH_OBJECTS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_BOTH_OBJECTS_MESSAGE"
placeholders: "<<DONE>> <<MISSING>> <<NAME>> <<OBJECTS>> <<WORD>>"
---
The question says '<<WORD>>' with no qualifier and no role word, and '<<NAME>>' is referenced on every object that word names: <<OBJECTS>>. Your answer counts only <<DONE>>. Run the same query on <<MISSING>> with the lookup that holds the person there (same grouping), then give each object's count on its own with its object and lookup; never add them. Leave an object out only when a result says so (the name or the field is not there, or the lookup has no data, or another word of the question already names one of them); a grouping field that is mostly blank is no reason: use the field its meaning names. Keep every record and count your answer already gives.
