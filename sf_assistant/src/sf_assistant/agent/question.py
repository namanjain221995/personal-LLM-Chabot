"""The :class:`Question` dataclass (moved from ``okeval/questions.py`` unchanged).

It lives in the agent layer because the knowledge agent, the record agent and the pipeline all build questions;
``sf_assistant.evaluation.questions`` re-exports it (one class).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

__all__ = ["Question"]


@dataclass
class Question:
    """One evaluation question.

    Attributes:
        qid: stable unique id (``<source>-<category>-<nnn>``; paraphrases append ``~p<j>``).
        category: one of :data:`CATEGORIES`.
        question: the natural-language question shown to the agent.
        expected_facts: short atomic strings a correct answer must contain (case-insensitive).
        expected_tools: tool names of which at least one should be called.
        answerable: False when the correct answer is "not in the knowledge file".
        source: ``"gold" | "paraphrase" | "llm" | "adversarial"``.
        gold_chunk_ids: knowledge chunk ids holding the answer (see SPEC chunk ids).
        exact_facts: subset of ``expected_facts`` that must match literally (an exact API
            token, or an exact picklist value without inflection); see ``grading``.
        forbidden_facts: confusable siblings (a near-miss API name, the "corrected" spelling
            of a typo'd API name, the opposite polarity) that fail the answer when asserted.
    """

    qid: str
    category: str
    question: str
    expected_facts: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    answerable: bool = True
    source: str = "gold"
    gold_chunk_ids: list[str] = field(default_factory=list)
    exact_facts: list[str] = field(default_factory=list)
    forbidden_facts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Plain-dict form (for JSONL export / DataFrames)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        """Inverse of :meth:`to_dict`; ignores unknown keys."""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})
