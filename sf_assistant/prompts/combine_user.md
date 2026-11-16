---
id: combine_user
version: 1
name: combine_user_message
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py combine_user_message (f-string layout)"
placeholders: "{facts} {field_check} {knowledge_answer} {question} {record_answer}"
fill: "filled by combine_user_message in one pass (values are never filled again); {field_check} is '' or '\\n\\nFIELD CHECK:' + one '\\n- <line>' per line (built in code)"
---
QUESTION:
{question}

KNOWLEDGE ANSWER:
{knowledge_answer}

RECORD ANSWER:
{record_answer}{field_check}

RECORD FACTS:
{facts}
