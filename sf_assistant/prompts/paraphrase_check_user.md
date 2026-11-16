---
id: paraphrase_check_user
version: 1
name: PARAPHRASE_CHECK_USER
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py PARAPHRASE_CHECK_USER"
placeholders: "{expected_facts} {object_name} {original} {rewrite}"
---
Two questions about the Salesforce object {object_name} are below.

<original>
{original}
</original>

<rewrite>
{rewrite}
</rewrite>

The original question is answered by these expected facts: {expected_facts}

Decide:
- same_information: does the rewrite ask for exactly the same information as the original (same
  field/object/automation, same aspect such as API name vs label vs meaning, same polarity)?
- facts_answer_rewrite: would an answer containing the expected facts fully and correctly answer
  the rewrite?
- reason: a few words.

Output ONLY one JSON object: {"same_information": true, "facts_answer_rewrite": true, "reason": ""}
