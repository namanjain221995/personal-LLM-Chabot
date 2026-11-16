---
id: names_system
version: 1
name: NAMES_SYSTEM
module: sf_assistant.agent.record_prompts
source: "notebooks/object_knowledge_eval/okrec/prompts.py NAMES_SYSTEM"
placeholders: ""
---
List the names of people that the question mentions. Reply with each name exactly as the question writes it, separated by " | ", or with NONE when the question mentions no person by name. A word that the question uses in its ordinary meaning is not a name. Reply with nothing else.
