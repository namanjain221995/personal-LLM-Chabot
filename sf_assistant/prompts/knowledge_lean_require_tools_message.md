---
id: knowledge_lean_require_tools_message
version: 1
name: KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py KNOWLEDGE_LEAN_REQUIRE_TOOLS_MESSAGE"
placeholders: "<<MAX_TOOLS>>"
---
You answered without a tool result. Call 1 to <<MAX_TOOLS>> tools now (search with the question's nouns and verb, not a person's name), then reply with the FACTS block from their results only.
