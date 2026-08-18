"""Best-of-N with a judge — how extra_high earns its name.

N candidates are generated CONCURRENTLY (asyncio.gather over full,
non-streaming completions that keep their reasoning), then a judge pass on
the same model — thinking OFF, guided JSON — picks the winner. The caller
streams the winner's thinking and answer to the UI; the losers are logged
at INFO for debugging, never shown.

Failure posture: best-of-N must never make extra_high WORSE than high. A
candidate that errors becomes an empty candidate; a judge that fails or
answers nonsense falls back to a deterministic pick (the longest answer);
zero usable candidates is the caller's signal to fall back to the ordinary
single-stream path.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .. import llm

log = logging.getLogger(__name__)

#: The judge reads ANSWERS, not reasoning: the verdict is about what the user
#: would receive, and N × 24k reasoning tokens would drown the judge prompt.
_JUDGE_ANSWER_CHARS = 4000

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "winner": {"type": "integer", "minimum": 1},
        "reason": {"type": "string"},
    },
    "required": ["winner"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = (
    "You are judging candidate answers to the same question. Pick the single "
    "best one: correct first, then complete, then clear. Respond with JSON "
    'only: {"winner": <candidate number>, "reason": "<one sentence>"}.'
)


@dataclass
class Candidate:
    index: int  # 1-based, stable across logs and judge verdicts
    reasoning: str = ""
    answer: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.answer.strip())


async def _generate_one(
    index: int,
    messages: Sequence[dict],
    *,
    temperature: float,
    max_tokens: Optional[int],
) -> Candidate:
    try:
        reasoning, answer = await llm.chat_completion_with_reasoning(
            messages,
            effort="extra_high",
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return Candidate(index=index, reasoning=reasoning, answer=answer)
    except Exception as exc:  # noqa: BLE001 — one bad sample must not kill the turn
        log.warning("best-of-N candidate %d failed: %s", index, str(exc)[:200])
        return Candidate(index=index, error=str(exc)[:300])


async def generate_candidates(
    messages: Sequence[dict],
    *,
    n: int,
    temperature: float,
    max_tokens: Optional[int],
) -> List[Candidate]:
    """N full candidates, generated concurrently — never sequentially."""
    return list(
        await asyncio.gather(
            *(
                _generate_one(
                    i + 1, messages, temperature=temperature, max_tokens=max_tokens
                )
                for i in range(n)
            )
        )
    )


async def select_best(
    question: str, candidates: Sequence[Candidate]
) -> Tuple[Candidate, str]:
    """→ (winning candidate, judge's reason). Never raises.

    The judge runs on the main model with thinking OFF and a guided-JSON
    verdict. Any judge failure — backend down, malformed verdict, an index
    that names no candidate — degrades to the longest usable answer, which
    is the best deterministic proxy for effort actually spent.
    """
    usable = [c for c in candidates if c.usable]
    if not usable:
        return candidates[0], "no candidate produced an answer"
    if len(usable) == 1:
        return usable[0], "only one candidate produced an answer"

    numbered = "\n\n".join(
        f"CANDIDATE {c.index}:\n{c.answer[:_JUDGE_ANSWER_CHARS]}" for c in usable
    )
    try:
        raw = await llm.json_completion(
            [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": f"QUESTION:\n{question}\n\n{numbered}",
                },
            ],
            json_schema=_JUDGE_SCHEMA,
            schema_name="verdict",
            temperature=0.0,
            max_tokens=300,
            thinking=False,
        )
        import json

        verdict = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        winner_index = int(verdict.get("winner", 0))
        reason = str(verdict.get("reason") or "").strip() or "judge verdict"
        for candidate in usable:
            if candidate.index == winner_index:
                return candidate, reason
        log.warning(
            "best-of-N judge named candidate %r which does not exist; "
            "falling back to longest answer",
            winner_index,
        )
    except Exception as exc:  # noqa: BLE001 — the judge is an optimization
        log.warning("best-of-N judge unavailable: %s", str(exc)[:200])

    fallback = max(usable, key=lambda c: len(c.answer))
    return fallback, "judge unavailable — longest answer kept"


def log_losers(candidates: Sequence[Candidate], winner: Candidate) -> None:
    """The losing candidates, at INFO, truncated — debugging, never UI."""
    for candidate in candidates:
        if candidate.index == winner.index:
            continue
        if candidate.error:
            log.info(
                "best-of-N candidate %d errored: %s", candidate.index, candidate.error
            )
        else:
            log.info(
                "best-of-N losing candidate %d (%d chars): %.300s",
                candidate.index,
                len(candidate.answer),
                candidate.answer,
            )
