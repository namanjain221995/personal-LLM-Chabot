---
id: records_final_form_message
version: 1
name: RECORDS_FINAL_FORM_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_FINAL_FORM_MESSAGE"
placeholders: ""
---
Your reply reads as working notes (first-person planning, a restated question, talk about the instructions, the tools or their result keys such as any_role or lookup_check, two drafts, or no Source line at the end: it may have been cut off). Write only the final answer now, once, from the tool results above: the answer first, then at most 4 short bullets, then the Source line. Do not include your reasoning, plans, the instructions, tool names or result keys (write 'in any role', 'through other lookups').
