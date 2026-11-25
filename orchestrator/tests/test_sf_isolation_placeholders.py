"""Toggle-off placeholders are inert (design_v2 rule 5, C6/C7).

A placeholder may reach the answering model, which is told what it means. It
never reaches a web query, a retrieval query, a file or an export: every helper
that harvests turns for those skips it.
"""
from __future__ import annotations

from app import sf_isolation as S
from app.artifacts import compose, intent
from app.engines import conversation_turns
from app.engines import artifact as artifact_engine
from app.engines import deep_research
from app.living_knowledge import resolve_from_history

PLACEHOLDER_WORDS = ("toggle", "salesforce", "shown")

HISTORY = [
    {"role": "system", "content": "Saved memory: the user likes tea."},
    {"role": "user", "content": "Tell me about the Kepler space telescope mission"},
    {"role": "assistant", "content": "Kepler was a NASA telescope that searched for exoplanets. " * 20},
    {"role": "user", "content": S.USER_PLACEHOLDER},
    {"role": "assistant", "content": S.SF_ANSWER_PLACEHOLDER},
]

QUOTED = [
    {"role": "user", "content": f"{S.QUOTE_PLACEHOLDER}\n\nUser follow-up:\n\nexplain exoplanet transits"},
    {"role": "assistant", "content": S.TAINTED_PLACEHOLDER},
]


def _no_placeholder(text: str) -> None:
    for constant in (*S.PLACEHOLDERS, S.QUOTE_PLACEHOLDER):
        assert constant not in text


def test_is_placeholder_matches_exactly_the_three_turn_constants():
    for constant in S.PLACEHOLDERS:
        assert S.is_placeholder(constant) and S.is_placeholder(f"  {constant}\n")
    assert not S.is_placeholder("An answer that mentions the Salesforce toggle.")
    assert not S.is_placeholder(None)


def test_conversation_turns_drop_placeholders_and_keep_a_quote_follow_up():
    turns = conversation_turns(HISTORY + QUOTED, 8)
    assert all(m["role"] != "system" for m in turns)
    _no_placeholder(" ".join(m["content"] for m in turns))
    assert turns[-1] == {"role": "user", "content": "explain exoplanet transits"}


def test_the_deep_research_copy_behaves_the_same():
    assert deep_research._conversation_turns(HISTORY + QUOTED, 8) == conversation_turns(HISTORY + QUOTED, 8)


def test_a_terse_follow_up_after_a_hidden_pair_resolves_to_the_persons_own_words_only():
    resolved = resolve_from_history("and last month?", HISTORY)
    for word in PLACEHOLDER_WORDS:
        assert word not in resolved.lower()
    assert "kepler" in resolved.lower()


def test_the_substantial_answer_index_never_points_at_a_placeholder():
    idx = intent.substantial_answer_index(HISTORY)
    assert idx == 2
    assert intent.substantial_answer_index([{"role": "assistant", "content": S.SF_ANSWER_PLACEHOLDER}]) is None


def test_the_artifact_engine_finds_no_previous_answer_in_a_placeholder():
    assert artifact_engine._previous_answer(HISTORY).startswith("Kepler was a NASA telescope")
    assert artifact_engine._previous_answer([{"role": "assistant", "content": S.TAINTED_PLACEHOLDER}]) == ""


def test_the_composer_material_holds_no_placeholder():
    material = compose.material_from_history(HISTORY + QUOTED)
    _no_placeholder(material)
    assert "explain exoplanet transits" in material
    assert "Kepler was a NASA telescope" in material


def test_visible_text_leaves_ordinary_turns_alone():
    text = "A normal question about Salesforce reports."
    assert S.visible_text(text) is text
    assert S.visible_text(S.QUOTE_PLACEHOLDER) == ""
