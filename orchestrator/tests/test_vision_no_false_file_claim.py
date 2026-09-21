"""The vision route never copies the app's file-card sentence (image-into-files
review, 2026-09-19: it answered "Created **Sharma Grocery Receipt Summary** as
PDF." with no file made)."""
from __future__ import annotations

from app.engines import vision


def test_file_card_lines_in_history_are_rewritten_for_the_vision_model():
    history = [
        {"role": "user", "content": "make a pdf of this receipt"},
        {"role": "assistant", "content": "Created **Sharma Grocery Receipt Summary** as PDF.\n_Some note._"},
        {"role": "user", "content": "and a word one?"},
        {"role": "assistant", "content": "Updated **Report** v2: 3 charts added."},
    ]
    turns = vision.history_turns(history)
    text = "\n".join(t["content"] for t in turns)
    assert "Created **" not in text and "Updated **" not in text
    assert "(Earlier the app made a file here: Sharma Grocery Receipt Summary.)" in text
    assert "_Some note._" in text
    # user turns and ordinary answers are untouched
    assert turns[0]["content"] == "make a pdf of this receipt"


def test_an_ordinary_answer_is_untouched():
    history = [{"role": "assistant", "content": "The total is **16.56**. Created in 2019, the shop..."}]
    assert vision.history_turns(history)[0]["content"] == history[0]["content"]
