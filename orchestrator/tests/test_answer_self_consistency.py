"""An answer must not argue with itself (2026-09-21).

THE DEFECT. An internal 114-run answer-quality sweep found 7 answers that
contradicted themselves. Two shapes: the headline sentence states a number or
a verdict that the body of the same answer then contradicts ("The total
revenue including 18% GST is **80,000**." above a breakdown totalling 79,945),
and a fact stated in one paragraph denied in a later one.

WHERE EACH HALF IS FIXED.

  * Where the platform owns the arithmetic — the generated dataset report,
    whose headline Total and whose breakdown rows are both written by
    engines/dataset_report.py — the gap is computed in Python and stated.
    Those tests live beside the renderer, in test_dataset_report.py
    (_reconciliation): no model is involved at all.

  * Where the model owns the arithmetic — the chat engine, which writes both
    the headline and the table — the fix is prompt-only, and it is a SCOPE on
    the clause that causes the failure rather than a new rule beside it.
    ASSISTANT_CONDUCT tells the model to "name one answer in the first
    sentence", which is right for a recommendation and wrong for a figure it
    has not worked out; the sentence pinned here says so.

WHAT IS PINNED HERE. That the scoping sentence reaches BOTH assistant
personas, and that it reaches NEITHER of the two short prompts — the Fast
small-talk lane and the Salesforce small-talk reply. A pleasantry cannot state
a total, and the lane prompt is on a character budget the latency work set
(test_fast_lane_classifier._LANE_SYSTEM_BUDGET): Fast mode must not pay for
this in prompt bytes any more than it pays for it in model calls.

The measurement behind the wording is recorded in ASSISTANT_CONDUCT itself.
"""
from __future__ import annotations

from app.engines import chat as chat_engine

#: The clause, exactly as it was measured.
CLAUSE = (
    "That is about a CHOICE, not about a figure. A total, a time, a count or "
    "a does-it-fit answer has to be worked out before it can be stated: put "
    "it after the rows, steps or table it comes from, and copy it from them. "
    "Never open with a figure you have not worked out yet."
)

#: The sentence it scopes. The two must stay adjacent: read apart, the first
#: is the instruction that produces the defect.
COMMIT = "one name and a price with nothing behind it is not an answer either."


def test_the_figure_clause_scopes_the_commit_rule_in_both_assistant_personas():
    """Assistant mode and Salesforce mode share ASSISTANT_CONDUCT, and an
    ordinary question in either one lands in the chat engine. A figure stated
    before it has been worked out is the same wrong answer in both."""
    assert CLAUSE in chat_engine.ASSISTANT_CONDUCT
    assert CLAUSE in chat_engine.ASSISTANT_SYSTEM
    assert CLAUSE in chat_engine.SALESFORCE_ASSISTANT_SYSTEM


def test_the_figure_clause_follows_the_commit_rule_it_scopes():
    conduct = chat_engine.ASSISTANT_CONDUCT
    assert COMMIT in conduct
    assert conduct.index(COMMIT) < conduct.index(CLAUSE)
    # Adjacent: nothing between the rule and the scope that narrows it.
    between = conduct[conduct.index(COMMIT) + len(COMMIT) : conduct.index(CLAUSE)]
    assert between.strip() == "", between


def test_the_figure_clause_never_reaches_a_small_talk_prompt():
    """Neither short prompt can be asked for a total, and the Fast lane's
    prompt is on a budget. Fast mode gains no prompt bytes here."""
    assert CLAUSE not in chat_engine.FAST_LANE_SYSTEM
    assert CLAUSE not in chat_engine.SALESFORCE_CHAT_SYSTEM
    lane = chat_engine._messages("hi", [], "assistant", lane="greeting")[0]["content"]
    assert CLAUSE not in lane
    # The lane persona's own share of the 700-character budget, with no
    # identity line and no saved facts: unchanged by this clause.
    assert len(chat_engine.FAST_LANE_SYSTEM) <= 700, len(chat_engine.FAST_LANE_SYSTEM)


def test_the_clause_costs_fast_mode_no_extra_model_call():
    """The whole fix for the chat half is text in a prompt that is already
    sent. Nothing here may introduce a second pass: run_chat_engine's Fast
    path must still be the one completion it was (the engine's own comment
    records why a bounded thinking grant was removed)."""
    import inspect

    source = inspect.getsource(chat_engine.run_chat_engine)
    # One streamed completion, plus the best-of branch that only `max` reaches.
    assert source.count("stream_long_completion") == 1
    assert "effort == \"max\"" in source
