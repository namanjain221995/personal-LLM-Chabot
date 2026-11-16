---
id: judge_user
version: 1
name: JUDGE_USER
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py JUDGE_USER"
placeholders: "{answerable} {answer} {category} {expected_facts} {gold_chunks} {question} {tool_calls}"
---
Question category: {category}
Answerable from the knowledge file: <answerable>{answerable}</answerable>

<question>
{question}
</question>

<expected_facts>
{expected_facts}
</expected_facts>

<gold_knowledge>
{gold_chunks}
</gold_knowledge>

<tool_calls>
{tool_calls}
</tool_calls>

<answer>
{answer}
</answer>

Grade the answer. Return JSON with exactly the keys correctness, completeness, groundedness,
hallucination, missing_knowledge, notes.
