---
id: records_all_failed_message
version: 2
name: RECORDS_ALL_FAILED_MESSAGE
module: sf_assistant.agent.record_prompts
source: "new in the prerequisites of 2026-11-25 (P2 records retry): sent with tool_choice required, as the one forced round, at the first draft check of a records-routed question when every record call failed and none failed for a name that is not stored, a blocked field or a fault of the engine (RecordSession.ask); v2 (review of 2026-11-25): sent only as the forced round, and no longer asks for a not-recorded sentence, which code dropped when no call gave data (the clarifying question is written instead)"
placeholders: ""
---
Every record call so far failed. Read the errors above: they name the closest objects, fields and values. Call the record tool again now with corrected arguments, then answer only from its result.
