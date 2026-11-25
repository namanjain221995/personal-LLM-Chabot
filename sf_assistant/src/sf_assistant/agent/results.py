"""The two result dataclasses of the record side (moved verbatim from okrec/session.py).

:class:`RecordAnswer` is the record agent's answer; :class:`MergeResult` is the merge call's result. With
``SFA_TABLES=code`` (latency plan step 3, 2026-10-29) a record answer also carries ``code_tables`` (the counts of its
table lines and code-built rows) and ``table_blocks`` (each table line and its rows as they stand in the answer, for
the merge call); both stay empty otherwise. In every mode it carries ``nudge_kinds``: the prompt id of each draft-check
message (step-3 fix T5).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sf_assistant.agent.knowledge_agent import AgentResult
from sf_assistant.engine.types import QueryEvidence


@dataclass
class RecordAnswer:
    """The record agent's outcome for one question."""

    question: str
    answer: str                    # the record agent's final answer ("" when no_records)
    no_records: bool               # NO_RECORDS reply, or no record tool call succeeded (unless not_found_reply)
    used_data: bool                # at least one DATA_TOOLS call succeeded
    evidence: list[QueryEvidence] = field(default_factory=list)
    agent: AgentResult | None = None
    error: str | None = None       # agent error or session failure
    unsupported_numbers: list[str] = field(default_factory=list)  # numbers in `answer` not found in the facts
    seconds: float = 0.0
    routed_out: bool = False       # the route call said the question needs no record data (the agent did not run)
    nudges: list[str] = field(default_factory=list)   # draft-check messages sent back to the agent (at most two)
    dropped_answer: str = ""       # the record agent's reply when it was dropped (a count after a not-found error)
    not_found_answer: str = ""     # code-built "no record is named ..." answer for a dropped reply (not_found_answer)
    dropped_numbers: list[str] = field(default_factory=list)  # numbers in no result that code removed (drop_unsupported)
    code_answer: bool = False      # code wrote the answer from about_people (the record agent gave none; §22.8)
    agent_error: str | None = None  # the record agent's error or NO_RECORDS when code wrote the answer
    named_people: list[str] = field(default_factory=list)   # the question's words for the people it names
    triage: dict = field(default_factory=dict)              # Triage.as_dict() (JSONL sidecar)
    # the stored spellings of a named person whose words are not stored as typed (a misspelt, reordered or shortened
    # name: the triage's close names and read_as), by those words: the people-answer fallback's coverage check
    named_spellings: dict[str, list[str]] = field(default_factory=dict)
    # SFA_TABLES=code (latency plan step 3): counts of the table lines and code rows (ints, fixed words, query ids)
    code_tables: dict = field(default_factory=dict)
    table_blocks: dict[str, str] = field(default_factory=dict)   # marker -> its code-built rows as they stand in answer
    nudge_kinds: list[str] = field(default_factory=list)   # the prompt id of each draft-check message, in order
    # the record agent's raw final text when its answer came back empty (the private log only, 2026-11-16: which
    # cleaner left nothing but the Source line)
    dropped_raw: str = ""
    # business-term contracts (2026-11-24): the Person Account / record-type scope claims no applied filter backed,
    # cut from the answer (agent.term_notes.drop_unapplied_scope_claims; also when the guard kept the answer whole)
    dropped_claims: list[str] = field(default_factory=list)
    # prerequisite P2 (2026-11-25): why the record step has no data, when it ran no record tool ("no_call") or every
    # call failed with no not-found or refusal code among them ("all_failed"); None otherwise. The pipeline then writes
    # the clarifying question (agent.clarify) instead of RECORDS_FAILED_ANSWER
    no_data: str | None = None
    # the one forced round of P2, when it ran: {"at": "tools_first" | "check", "why": "no_call" | "no_records" |
    # "all_failed" | "no_data_call" (numbers after describe_fields or the sync status only), "choice": "required" |
    # "auto_fallback" | "auto_off"}; None otherwise
    tool_retry: dict | None = None

    @property
    def data_queries(self) -> int:
        """Successful data-tool calls."""
        return sum(1 for e in self.evidence if e.ok and e.kind == "data")


# --------------------------------------------------------------------------------------- merge

@dataclass
class MergeResult:
    """The final answer after the merge call and the number audit."""

    text: str                      # the final answer printed first
    merged: bool                   # True when the merged text passed the audit
    rejected_numbers: list[str] = field(default_factory=list)  # numbers that failed the audit (merged=False)
    error: str | None = None
    seconds: float = 0.0
