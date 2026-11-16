---
id: paraphrase_user
version: 1
name: PARAPHRASE_USER
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py PARAPHRASE_USER"
placeholders: "{n} {question}"
---
Rewrite the question below in exactly {n} different ways.

Requirements:
- Use everyday business language (recruiting, interviews, scheduling, scores, feedback).
- Do NOT use API names: nothing ending in __c, no underscores, no CamelCase identifiers such as
  CreatedDate. Use the plain-English label or a description of the concept instead.
- Keep the same meaning and the same answer; do not make it broader, narrower or multi-part.
- Make the {n} rewrites clearly different from each other (wording, structure, perspective).

<question>
{question}
</question>

Return JSON with exactly this shape:
{"paraphrases": ["...", "..."]}
