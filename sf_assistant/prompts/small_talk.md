---
id: small_talk
version: 2
name: SMALL_TALK_TEXT
module: sf_assistant.agent.small_talk
source: "new in the prerequisites of 2026-11-25 (P6 small talk): the fixed line code sends for a greeting, thanks, an acknowledgement, a goodbye, a presence check, an empty ask or a help ask, with no model call (agent.small_talk); v2 (review of 2026-11-25): the help line promises no charts, which SFA_CHARTS=off turns off"
placeholders: ""
fill: "one line per kind as '<kind>: <text>', read into a dict by agent.small_talk (bound as this raw text); no placeholders"
---
greeting: Hello! Ask me about your Salesforce records or fields, for example: how many interviews were there last month?
thanks: You're welcome. Ask me anything else about your Salesforce records or fields.
ack: Okay. Ask your next question when you are ready.
bye: Goodbye.
presence: Yes, I'm here. What would you like to know about your Salesforce records or fields?
empty_ask: Sure, go ahead: ask your question, with the object, the measure and the period if there is one.
help: I answer questions about your Salesforce data from a synced copy: counts, lists, breakdowns and trends of records, and what objects and fields mean. For example: how many interviews were there per month this year?
