"""Agents: the knowledge agent, the record agent (record_session), lean step, merge, prompts, LLM client.

Record side (split of okrec/session.py): ``checks`` (pure text checks and cleaners) < ``results`` (RecordAnswer,
MergeResult) < ``nudges`` (draft checks) < ``triage`` (names, Triage) < ``people_answer`` < ``merge`` <
``record_session`` (RecordSession; re-exports every moved name). Nothing is re-exported from this package.
"""
