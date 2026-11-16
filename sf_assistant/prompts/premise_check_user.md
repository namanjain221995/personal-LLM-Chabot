---
id: premise_check_user
version: 1
name: PREMISE_CHECK_USER
module: sf_assistant.agent.knowledge_prompts
source: "notebooks/object_knowledge_eval/okeval/prompts.py PREMISE_CHECK_USER"
placeholders: "{excerpts} {object_name} {question}"
---
A test question was written about the Salesforce object {object_name} from the knowledge
excerpts below. Check its presupposition: everything the question takes for granted (that a
field, value, automation or statement exists, or that the file says something) must be true
according to the excerpts.

<question>
{question}
</question>

<knowledge_excerpts>
{excerpts}
</knowledge_excerpts>

Output ONLY one JSON object: {"premise_true": true, "reason": ""}
