---
id: question_gen_user
version: 1
name: QUESTION_GEN_USER
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py QUESTION_GEN_USER"
placeholders: "{category_definition} {category} {excerpts} {n} {object_api_name}"
---
Object: {object_api_name}
Category: {category}
Category definition: {category_definition}

Generate exactly {n} questions for category `{category}` using only these knowledge excerpts:
<knowledge_excerpts>
{excerpts}
</knowledge_excerpts>

Return JSON with exactly this shape:
{"questions": [{"question": "...", "category": "{category}", "expected_facts": ["..."], "expected_tools": ["..."], "answerable": true}]}
