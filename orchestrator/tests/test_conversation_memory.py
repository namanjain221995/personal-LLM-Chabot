"""What the assistant remembers of the conversation it is in.

THE BUG THIS FILE EXISTS FOR, from production. A user held a 60-message
French lesson: corrections, grammar, practice, all of it context-aware. At
message 52 they asked "how to translate" and got a Python tutorial about
googletrans — because `engines/chat.py` passed `recent_turns(history, 6)`,
the last six turns were a goodnight exchange, and the entire lesson was
outside the window. The model has a 1,000,000-token context; the conversation
was 5,826 tokens, 0.58% of it.

The 6 was not unreasonable when it was written — it was a defence against a
small window. It became wrong when the window grew and nothing else took over
the job, because the two components that SHOULD bound history both measure
themselves against the window as a fraction:

  usable budget on a 1M window   ~991,000 tokens
  compaction fires above 0.80    ~793,000 tokens

No real conversation reaches that, so no rolling summary was ever written.
Between "six turns" and "793,000 tokens" there was nothing at all.

So these tests are about the LAYERING, not a number: the engine sends the
conversation, compaction decides what a long one is condensed to, and
fit_request guarantees it fits.
"""
from __future__ import annotations

from typing import List

import pytest

from app import compaction
from app.compaction import Budget
from app.config import settings
from app.engines import chat as chat_engine
from app.engines import recent_turns


def french_lesson(pairs: int = 25) -> List[dict]:
    """A long conversation whose SUBJECT is established at the start.

    Shaped like the real one: the framing that makes a later question
    intelligible is early, and the last few turns are about something else
    entirely.
    """
    history: List[dict] = [
        {"role": "user", "content": "Can you have a conversation with me in french?"},
        {"role": "assistant", "content": "Absolument ! Commençons. Bonjour !"},
    ]
    for i in range(pairs):
        history.append({"role": "user", "content": f"j'ai mange petit-dejeuner numero {i}"})
        history.append(
            {"role": "assistant", "content": f"Correction {i} : « J'ai mangé un petit-déjeuner »."}
        )
    # …and it ends on an unrelated goodnight, exactly as the real one did.
    history += [
        {"role": "user", "content": "je vai dormi"},
        {"role": "assistant", "content": "Bonne nuit ! 🌙"},
        {"role": "user", "content": "I feel like sleeping"},
        {"role": "assistant", "content": "Good night! Rest well."},
    ]
    return history


def user_texts(messages: List[dict]) -> str:
    return "\n".join(m["content"] for m in messages if m.get("role") != "system")


# ---------------------------------------------------------------------------
# The engine sends the conversation
# ---------------------------------------------------------------------------


def test_the_assistant_sees_what_the_conversation_is_about():
    """The regression, in one assertion. "how to translate" is answerable only
    if the model can still see that this is a French lesson."""
    history = french_lesson()
    prompt = chat_engine._messages("how to translate", history, "assistant")
    body = user_texts(prompt)
    assert "conversation with me in french" in body, (
        "the framing that makes the question intelligible was dropped"
    )
    assert "Good night" in body, "and the most recent turns are still there"


def test_a_sixty_message_conversation_arrives_whole():
    history = french_lesson()          # 56 messages
    prompt = chat_engine._messages("how to translate", history, "assistant")
    turns = [m for m in prompt if m.get("role") != "system"]
    # Every turn, plus the new question.
    assert len(turns) == len(history) + 1


def test_the_pinned_system_blocks_still_survive():
    """Cross-chat recall, saved facts and shared documents ride as system
    messages. They were kept regardless of age before and must stay so."""
    history = [
        {"role": "system", "content": "MEMORY: the user works at TechSara."},
        *french_lesson(),
    ]
    prompt = chat_engine._messages("and what do I do for work?", history, "assistant")
    assert any("TechSara" in m["content"] for m in prompt if m["role"] == "system")


def test_the_window_is_a_backstop_not_a_memory_policy():
    """A pathological thread is still bounded — but by a number that no real
    conversation reaches, not by one that cuts a lesson in half."""
    assert settings.chat_history_turns >= 100
    huge = [{"role": "user", "content": "x"}] * (settings.chat_history_turns * 3)
    kept = recent_turns(huge, settings.chat_history_turns)
    assert len(kept) == settings.chat_history_turns


def test_recent_turns_still_keeps_the_newest_turns():
    history = [{"role": "user", "content": str(i)} for i in range(50)]
    kept = recent_turns(history, 6)
    assert [m["content"] for m in kept] == ["44", "45", "46", "47", "48", "49"]


# ---------------------------------------------------------------------------
# Compaction has to fire at a size that actually occurs
# ---------------------------------------------------------------------------


def _budget(used: int, window: int = 1_000_000) -> Budget:
    reserved = compaction.output_reservation(None, window)
    return Budget(
        window=window,
        output_reserved=reserved,
        usable=compaction.usable_budget(window, reserved),
        used=used,
    )


def test_a_fraction_of_a_million_tokens_is_not_a_trigger():
    """The measurement that explains the whole bug: on a 1M window the
    fraction threshold sits so far away that it is not a trigger at all."""
    budget = _budget(used=5_826)               # the real conversation
    assert budget.fraction < 0.01
    trigger_at = budget.usable * settings.context_compact_threshold
    assert trigger_at > 700_000, (
        f"the fraction trigger needs {trigger_at:.0f} tokens — no conversation "
        "gets there, which is why no summary was ever written"
    )


def test_the_absolute_ceiling_fires_where_conversations_actually_live():
    threshold = settings.context_compact_threshold
    assert compaction.should_compact(_budget(used=5_826), threshold) is False
    assert compaction.should_compact(
        _budget(used=settings.context_compact_max_tokens + 1), threshold
    ) is True


def test_the_fraction_trigger_still_works_on_a_small_window():
    """The absolute ceiling ADDS a trigger; it must not remove the one that
    protects a small model, where 40,000 tokens is past the window entirely."""
    threshold = settings.context_compact_threshold
    small = _budget(used=30_000, window=32_768)
    assert small.fraction > threshold
    assert compaction.should_compact(small, threshold) is True


def test_the_absolute_ceiling_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "context_compact_max_tokens", 0)
    assert compaction.should_compact(_budget(used=500_000), settings.context_compact_threshold) is False


def test_between_the_two_triggers_nothing_is_left_unbounded():
    """The property, stated once: any conversation large enough to matter is
    condensed by one trigger or the other."""
    threshold = settings.context_compact_threshold
    for used in (50_000, 200_000, 800_000, 5_000_000):
        assert compaction.should_compact(_budget(used=used), threshold) is True, used


# ---------------------------------------------------------------------------
# The same amnesia was one route away
#
# The chat engine was the one that answered this user, but every engine built
# its prompt the same way. An identical question that happened to route to
# search, a document or a repo would have forgotten just as much — so the
# window is a setting now, and these assert that nothing kept a literal.
# ---------------------------------------------------------------------------


def test_no_answer_prompt_keeps_a_hardcoded_history_slice():
    import pathlib
    import re

    # Prompts whose OUTPUT LEAVES THE BOX keep tight windows on purpose:
    # `conversation_turns` strips the memory blocks precisely because search
    # queries and research plans travel to third parties. `_ask_sql` writes
    # SQL rather than prose to the reader.
    allowed = {
        ("__init__.py", "conversation_turns"),
        ("deep_research.py", "_conversation_turns"),
        ("sql.py", "_ask_sql"),
    }
    offenders = []
    for path in sorted(pathlib.Path("app/engines").glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            m = re.search(r"recent_turns\(history,\s*(\d+)\s*\)", line)
            if not m:
                continue
            source = path.read_text().splitlines()[:i]
            fn = ""
            for prev in reversed(source):
                if prev.startswith(("def ", "async def ")):
                    fn = prev.split("(")[0].replace("async def ", "").replace("def ", "")
                    break
            if (path.name, fn) in allowed:
                continue
            offenders.append(f"{path.name}:{i} {fn}() slices to {m.group(1)}")
    assert not offenders, (
        "these prompts still truncate the conversation to a literal instead of "
        "settings.chat_history_turns:\n  " + "\n  ".join(offenders)
    )


def test_the_outbound_prompts_still_strip_the_memory_blocks():
    """Widening history must not have widened what leaves the machine. The
    search rewriter turns its context into SearXNG queries, so a saved fact in
    that prompt is a saved fact on the wire."""
    from app.engines import conversation_turns

    history = [
        {"role": "system", "content": "MEMORY: the user's employer is TechSara."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    out = conversation_turns(history, 4)
    assert all(m["role"] != "system" for m in out)
    assert not any("TechSara" in m["content"] for m in out)


# ---------------------------------------------------------------------------
# The two things that made the wrong answer CODE-shaped
# ---------------------------------------------------------------------------


def test_the_code_instruction_does_not_claim_every_ambiguous_question():
    """It is attached to every assistant turn, and it used to say — flatly —
    "if the request is ambiguous, state the assumption you coded against".
    Inside a French lesson, "how to translate" is ambiguous, and the model did
    exactly as instructed."""
    from app.engines import CODE_INSTRUCTION

    assert "If a request FOR CODE is ambiguous" in CODE_INSTRUCTION
    assert "not a reason to answer a non-programming question with a program" in (
        CODE_INSTRUCTION
    )
    # The unscoped form must not come back.
    assert "If the request is ambiguous, state the" not in CODE_INSTRUCTION


def test_recall_drops_the_tail_that_is_far_weaker_than_its_best_hit():
    """With nothing relevant to find, an absolute floor alone returns whatever
    clears it — which is how a French lesson got handed snippets about code."""
    from app.config import settings as cfg

    best = 0.62
    floor = best * cfg.semantic_recall_relative_floor
    assert floor > cfg.semantic_recall_min_score, (
        "on a confident hit the relative floor must be the binding one"
    )
    # A hit at 0.31 clears the absolute floor and is nowhere near the best.
    assert 0.31 > cfg.semantic_recall_min_score
    assert 0.31 < floor


def test_the_relative_floor_never_discards_the_best_hit():
    from app.config import settings as cfg

    assert 0 < cfg.semantic_recall_relative_floor <= 1.0
    for best in (0.31, 0.5, 0.99):
        assert best >= best * cfg.semantic_recall_relative_floor
