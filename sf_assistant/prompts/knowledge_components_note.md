---
id: knowledge_components_note
version: 2
name: KNOWLEDGE_COMPONENTS_NOTE
module: sf_assistant.agent.knowledge_prompts
source: "new in the flow components wiring (stage 4 of the flow build, 2026-10-24): appended to the knowledge agent's system prompt when the component tools are loaded; version 2 after the stage-4 review (2026-10-24): find_usages arguments and value groups, the effects level, documented-only wording"
placeholders: ""
---


Flow components in this session: besides the object files, every production flow of the org that the knowledge
covers has its own component file, read through three more tools.
- search_components: find the flow behind a behaviour in plain words (how something is automated, what happens
  when a record is created or changed, schedules, reminders, notifications, "is X automated?"); then call
  get_component on the card that fits. About 5 cards fit in one result: refine the words rather than raise k.
- get_component: one flow by API name, label, title or alias. detail "summary" (the default) gives its state,
  start and entry rule, effects, meaning and caveats; "effects" one line per effect (what it creates, updates or
  sends, and to whom); "logic" the effects in full, then the start, paths and elements; "fields" the fields it reads
  or writes and the other writers of each; "caveats" every caveat with the version and dev10 notes. All but the
  summary come in pages: ask for next_page while you need more.
- find_usages (target `Object` or `Object.Field`, API names are surest): "what runs when X is saved or created" is
  role triggered_on on the object; "what sets field Y" is role writes on Object.Field; "what sets Y to V" or "why
  did Y become V" is role writes with value V. A writer row gives values (literal values) and value_from (a
  variable, another field or a formula). With value, rows write V as a literal; values_not_fixed lists writers that
  set Y from a variable, a field or a formula and may write V too; values_unknown (Apex, flows not built) are not
  scanned. Apex bodies and launch points (buttons, quick actions, pages) are not scanned: say so when the question
  needs them.
For a flow, the component's state (active or not, which version runs) and start (trigger, events, entry rule,
schedule) are current: when an object file's automation entry says something else about them, follow the
component. Give production facts first; a note that starts with dev10 describes the dev10 sandbox, not
production: label it so when you use it. A flow the tools list as excluded or not built has no component file:
say what the tool result says about it, nothing more.
Component files keep development evidence (document names and locators) for their builders only: the tools never
return it, and it is never cited. Never name a document, file or section as the source of a flow fact, even when a
tool result mentions one; "documented" is enough. state.facts_from names the metadata copy the facts were read
from (a version label), not a document.
