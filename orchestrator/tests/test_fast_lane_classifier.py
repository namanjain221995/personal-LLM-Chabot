"""The Fast small-talk lane's decision (app/fast_lane.py), offline.

Zero false positives is the hard gate: a real request answered as small talk
is the failure this lane must never produce, while a pleasantry that misses
the lane costs only the normal path's latency. So every FULL_PATH item of the
labelled set, every live-value string the freshness suites guard, and each of
those wrapped in a greeting or thanks must be vetoed; every FAST_LANE item
must enter with any ordinary history, and none may enter after a turn that
went unanswered.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import fast_lane, llm, metrics
from app.engines import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION
from app.engines import chat as chat_engine
from app.facts import FACTS_HEADER
from tests.test_freshness import CASES as FRESHNESS_CASES
from tests.test_freshness_time_signal import LIVE_VALUE
from tests.test_living_knowledge_fast_budget import (
    LIVE_IN_A_TIMELESS_SHAPE,
    LIVE_WITHOUT_A_RECENCY_WORD,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "fast_lane_guard_set.json"
_ITEMS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["items"]
LANE_ITEMS = [i for i in _ITEMS if i["expected"] == "FAST_LANE"]
FULL_PATH_ITEMS = [i for i in _ITEMS if i["expected"] == "FULL_PATH"]


def _request(text: str, **overrides) -> SimpleNamespace:
    """The ChatRequest attributes fast_lane.decide reads, as a Fast assistant
    text turn. Field names are pinned to the real model below."""
    fields = dict(
        text=text.strip(),
        effort="fast",
        mode="assistant",
        pdf_data=None,
        pdf_uploads=None,
        video_uploads=None,
        image_data=None,
        agent=False,
        deep_research=False,
        web_search="auto",
        sf_live=False,
        clarification=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _decide(text: str, history=(), **overrides) -> fast_lane.LaneDecision:
    req = _request(text, **overrides)
    return fast_lane.decide(req, text=req.text, history=list(history), now_year=2026)


OFFICE_HISTORY = [
    {"role": "user", "content": "How do I lock the header row in a spreadsheet?"},
    {"role": "assistant", "content": "Select the row below it and choose View > Freeze panes."},
]
OFFER_HISTORY = [
    {"role": "user", "content": "I need slides for the quarterly review."},
    {"role": "assistant", "content": "I can draft an outline with six sections. Shall I build the deck?"},
]
CLOSING_HISTORY = [
    {"role": "user", "content": "thanks for the outline"},
    {"role": "assistant", "content": "You're welcome! Let me know if you need anything else."},
]
GREETED_HISTORY = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "Hello! How can I help you today? 😊"},
]
#: A pleasantry after an open offer or question may be its ACCEPTANCE. Live
#: (2026-09-15): "thanks" after the script offer below made the lane write the
#: whole script under its 1024-token cap with no code rules.
OFFER_HISTORIES = {
    "shall_i": OFFER_HISTORY,
    "want_me_to": [
        {"role": "user", "content": "I need a python script that parses web server logs"},
        {"role": "assistant", "content": "Plan: regex per line, count with Counter. Want me to write the full script?"},
    ],
    "live_value_offer": [
        {"role": "user", "content": "how has gold done this year"},
        {"role": "assistant", "content": "It rose strongly. Shall I check today's gold rate in your city for you?"},
    ],
    "no_question_mark": [
        {"role": "user", "content": "draft a leave policy"},
        {"role": "assistant", "content": "Here is a first draft.\n\nLet me know if you'd like me to add a section on sick leave."},
    ],
    "if_youd_like": [
        {"role": "user", "content": "explain SPF records"},
        {"role": "assistant", "content": "SPF lists the servers allowed to send mail. If you'd like, I can check your record."},
    ],
    "open_question": [
        {"role": "user", "content": "book me a hotel"},
        {"role": "assistant", "content": "Which city and which dates?"},
    ],
    "hindi_question": [
        {"role": "user", "content": "leave application likho"},
        {"role": "assistant", "content": "यह रहा ड्राफ्ट। क्या मैं इसे अंग्रेज़ी में भी लिखूं?"},
    ],
}
UNANSWERED_HISTORIES = {
    "user_question_without_answer": [
        {"role": "user", "content": "what is the gold price today?"},
    ],
    "user_question_after_an_answered_one": [
        *OFFICE_HISTORY,
        {"role": "user", "content": "and what is the dollar rate right now?"},
    ],
    "empty_assistant_turn": [
        {"role": "user", "content": "what is the gold price today?"},
        {"role": "assistant", "content": "   "},
    ],
    "errored_assistant_turn": [
        {"role": "user", "content": "what is the gold price today?"},
        {"role": "assistant", "content": "The gold price today is", "meta": {"error": {"code": "MODEL_DIED"}}},
    ],
    "interrupted_assistant_turn": [
        {"role": "user", "content": "who won the match yesterday?"},
        {"role": "assistant", "content": "The match", "meta": {"status": "interrupted"}},
    ],
    "cancelled_assistant_turn": [
        {"role": "user", "content": "who won the match yesterday?"},
        {"role": "assistant", "content": "The match", "meta": {"cancelled": True}},
    ],
}


# ---------------------------------------------------------------------------
# The labelled set
# ---------------------------------------------------------------------------


def test_the_guard_set_is_what_this_round_labelled():
    assert len(_ITEMS) >= 200
    assert LANE_ITEMS and FULL_PATH_ITEMS
    # Acknowledgements are full path this round, whatever their original label.
    acks = [i for i in _ITEMS if i["category"] == "ack_full_path_this_round"]
    assert {"ok", "got it", "sounds good", "noted", "👍", "🙏", "theek hai", "accha"} <= {i["text"] for i in acks}
    assert all(i["expected"] == "FULL_PATH" for i in acks)
    added = {i["text"] for i in FULL_PATH_ITEMS}
    for text in (
        "again", "a lot", "so much", "there", "ok", "okay", "cool", "🙏", "lol?",
        "hi hi is the market open", "hello, what is the dollar rate", "HI sales tax",
        "hi claude can you check aws status",
    ):
        assert text in added, text
    for item in LANE_ITEMS:
        assert item["lane_category"] in fast_lane.CATEGORIES, item


ANSWERED_HISTORIES = {
    "no_history": [],
    "office_history": OFFICE_HISTORY,
    "after_closing": CLOSING_HISTORY,
    "after_greeting": GREETED_HISTORY,
}


# One test per labelled item; the history shapes are looped inside it (the
# suite's per-test database fixture makes a test per combination cost ~0.1 s
# for nothing this module reads).
@pytest.mark.parametrize("item", LANE_ITEMS, ids=lambda i: f"{i['id']}:{i['text']}")
def test_every_lane_item_enters_after_an_answered_turn_and_never_after_an_unanswered_one(item):
    for name, history in ANSWERED_HISTORIES.items():
        decision = _decide(item["text"], history)
        assert decision == fast_lane.LaneDecision(True, item["lane_category"], "none"), (name, item)
    for name, history in UNANSWERED_HISTORIES.items():
        decision = _decide(item["text"], history)
        assert decision == fast_lane.LaneDecision(False, item["lane_category"], "unanswered_previous"), (
            name,
            item,
        )
    for name, history in OFFER_HISTORIES.items():
        decision = _decide(item["text"], history)
        assert decision == fast_lane.LaneDecision(False, item["lane_category"], "pending_offer"), (name, item)


@pytest.mark.parametrize("item", FULL_PATH_ITEMS, ids=lambda i: f"{i['id']}:{i['text']}")
def test_every_full_path_item_is_vetoed(item):
    for name, history in {**ANSWERED_HISTORIES, **UNANSWERED_HISTORIES, **OFFER_HISTORIES}.items():
        decision = _decide(item["text"], history)
        assert not decision.entered, (name, item, decision)
        assert decision.veto in fast_lane.VETOES and decision.veto != "none"


# ---------------------------------------------------------------------------
# Live values, alone and wrapped in a pleasantry
# ---------------------------------------------------------------------------

_LIVE_STRINGS = sorted(
    set(LIVE_VALUE)
    | set(LIVE_WITHOUT_A_RECENCY_WORD)
    | set(LIVE_IN_A_TIMELESS_SHAPE)
    | {question for question, _level in FRESHNESS_CASES}
)


def test_the_imported_live_value_lists_are_not_empty():
    assert len(LIVE_VALUE) > 10 and len(LIVE_WITHOUT_A_RECENCY_WORD) > 5
    assert len(LIVE_IN_A_TIMELESS_SHAPE) > 5 and len(FRESHNESS_CASES) > 20


@pytest.mark.parametrize(
    "wrap",
    [lambda q: q, lambda q: "hi, " + q, lambda q: q + " thanks", lambda q: "namaste " + q],
    ids=["alone", "hi_comma_prefix", "thanks_suffix", "namaste_prefix"],
)
def test_no_live_value_or_freshness_case_enters_alone_or_wrapped(wrap):
    entered = [wrap(q) for q in _LIVE_STRINGS if _decide(wrap(q)).entered]
    assert entered == []


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, veto",
    [
        (dict(effort="think"), "not_fast"),
        (dict(effort="max"), "not_fast"),
        (dict(mode="salesforce"), "not_assistant"),
        (dict(pdf_data="JVBERi0="), "attachment"),
        (dict(pdf_uploads=[{"upload_id": "u1"}]), "attachment"),
        (dict(video_uploads=[{"upload_id": "v1"}]), "attachment"),
        (dict(image_data="iVBORw0KGgo="), "attachment"),
        (dict(agent=True), "flags"),
        (dict(deep_research=True), "flags"),
        (dict(web_search="on"), "flags"),
        (dict(sf_live=True), "sf"),
        (dict(clarification={"intent_id": "i1", "answers": {}}), "sf"),
    ],
)
def test_every_request_gate_vetoes_a_plain_greeting(overrides, veto):
    assert _decide("hello", **overrides) == fast_lane.LaneDecision(False, "none", veto)


def test_the_text_must_be_the_request_text_unchanged():
    req = _request("hello")
    rewritten = fast_lane.decide(req, text="hello (Clarified: EMEA)", history=[], now_year=2026)
    assert rewritten.veto == "flags" and not rewritten.entered


@pytest.mark.parametrize(
    "text, veto",
    [
        ("hi " + "there " * 10, "too_long"),
        ("x" * 500, "too_long"),
        ("hi 5", "digit"),
        ("hello https://example.com", "url"),
        ("hello", None),
    ],
)
def test_length_digit_and_url_gates(text, veto):
    decision = _decide(text)
    if veto is None:
        assert decision.entered
    else:
        assert decision.veto == veto and not decision.entered


def test_the_defence_in_depth_checks_veto_a_lexicon_hit(monkeypatch):
    from app import freshness
    from app.artifacts import intent as artifact_intent
    from app.engines import video

    assert _decide("hello").entered
    monkeypatch.setattr(freshness, "_live_signal", lambda q: True)
    assert _decide("hello") == fast_lane.LaneDecision(False, "greeting", "live_signal")
    monkeypatch.undo()

    real = artifact_intent.decide
    monkeypatch.setattr(
        artifact_intent, "decide", lambda text, **kw: real("make a pdf of the report", **kw)
    )
    assert _decide("hello") == fast_lane.LaneDecision(False, "greeting", "artifact_intent")
    monkeypatch.undo()

    import re

    monkeypatch.setattr(video, "_ABOUT_VIDEO_RE", re.compile(r"hello"))
    assert _decide("hello") == fast_lane.LaneDecision(False, "greeting", "cue")


def test_capitalised_words_mid_message_count_as_a_name():
    # "Hi There" reads like a name to the freshness rules; the lane defers.
    assert _decide("Hi There").veto == "live_signal"
    assert _decide("HELLO").entered


def test_the_kill_switch(monkeypatch):
    monkeypatch.setenv("FAST_LANE_ENABLED", "false")
    assert _decide("hello") == fast_lane.LaneDecision(False, "none", "disabled")
    monkeypatch.setenv("FAST_LANE_ENABLED", "0")
    assert _decide("thanks").veto == "disabled"
    monkeypatch.setenv("FAST_LANE_ENABLED", "true")
    assert _decide("hello").entered
    monkeypatch.delenv("FAST_LANE_ENABLED")
    assert _decide("hello").entered


def test_the_request_fields_the_lane_reads_exist_on_the_real_chat_request():
    from app.main import ChatRequest

    req = ChatRequest(message="hello", mode="assistant", effort="fast")
    for name in (
        "text", "effort", "mode", "pdf_data", "pdf_uploads", "video_uploads", "image_data",
        "agent", "deep_research", "web_search", "sf_live", "clarification",
    ):
        assert hasattr(req, name), name
    assert fast_lane.decide(req, text=req.text, history=[], now_year=2026).entered


# ---------------------------------------------------------------------------
# The classifier's own rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, category",
    [
        ("hi ??", "greeting"),
        ("hello?", "greeting"),
        ("Hello!!", "greeting"),
        ("good evening ji", "greeting"),
        ("hey there 👋🏽", "greeting"),
        ("thank you so much 🙏", "thanks"),
        ("thanks again", "thanks"),
        ("thank you a lot", "thanks"),
        ("see you later bhai", "farewell"),
        ("hahahaha", "laughter"),
        ("😂 😂", "emoji"),
        ("❤️", "emoji"),
        ("how’s it going", "greeting"),
        # Everything below is not a pleasantry, or not one this round.
        ("lol?", None),
        ("thanks?", None),
        ("bye?", None),
        ("😂?", None),
        ("??", None),
        ("?", None),
        ("…", None),
        ("🤔", None),
        ("🙏", None),
        ("👍", None),
        ("👌", None),
        ("again", None),
        ("a lot", None),
        ("so much", None),
        ("there", None),
        ("hello again", None),
        ("ok", None),
        ("okay", None),
        ("cool", None),
        ("great", None),
        ("nice", None),
        ("awesome", None),
        ("perfect", None),
        ("got it", None),
        ("sounds good", None),
        ("noted", None),
        ("theek hai", None),
        ("accha", None),
        ("haan", None),
        ("majama", None),
        ("yes", None),
        ("no", None),
        ("sure", None),
        ("why", None),
        ("kyu", None),
        ("kem", None),
        ("sup", None),
        ("yo", None),
        ("ty", None),
        ("hi hi", None),
        ("hi?!?", None),
        ("नमस्ते 🙏", None),
        ("", None),
    ],
)
def test_classify_pleasantry(text, category):
    assert fast_lane.classify_pleasantry(text) == category


def test_the_lexicon_is_not_freshness_small_talk(monkeypatch):
    """_SMALL_TALK fullmatches "ok", "cool", "again" and "a lot"; the lane
    must not follow it, whatever it matches."""
    import re

    from app import freshness

    monkeypatch.setattr(freshness, "_SMALL_TALK", re.compile(r".*", re.S))
    monkeypatch.setattr(freshness, "clearly_timeless", lambda *a, **k: True)
    for text in ("ok", "cool", "again", "a lot", "so much", "there", "sup", "yo", "ty"):
        assert _decide(text).veto == "not_lexicon", text


def test_the_metric_vocabulary_matches_the_module():
    assert set(fast_lane.VETOES) == metrics.FAST_LANE_VETOES
    assert set(fast_lane.CATEGORIES) | {"none"} == metrics.FAST_LANE_CATEGORIES
    assert "small_talk_lane" in metrics.KNOWLEDGE_DECISIONS


def test_record_counts_under_closed_labels():
    metrics.reset()
    fast_lane.record(fast_lane.LaneDecision(True, "greeting", "none"))
    fast_lane.record(fast_lane.LaneDecision(False, "none", "not_lexicon"))
    fast_lane.record(fast_lane.LaneDecision(False, "What is gold today?", "free text"))
    keys = set(metrics._counters["fast_lane_total"])
    assert (("category", "greeting"), ("result", "entered"), ("veto", "none")) in keys
    assert (("category", "none"), ("result", "vetoed"), ("veto", "not_lexicon")) in keys
    assert (("category", "other"), ("result", "vetoed"), ("veto", "other")) in keys


# ---------------------------------------------------------------------------
# The lane's prompt (engines/chat.py)
# ---------------------------------------------------------------------------


def test_the_lane_prompt_is_the_persona_the_facts_and_two_clipped_exchanges():
    history = [
        {"role": "system", "content": "Relevant notes from the user's other conversations: the audit"},
        {"role": "system", "content": FACTS_HEADER + "\n- Prefers short answers"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question " + "x" * 3000},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "third question"},
        {"role": "assistant", "content": "third answer"},
    ]
    messages = chat_engine._messages("hi ??", history, "assistant", lane="greeting")
    system = messages[0]
    assert system["role"] == "system"
    assert system["content"].startswith(chat_engine.ASSISTANT_SYSTEM)
    assert "Prefers short answers" in system["content"]
    assert "other conversations" not in system["content"]
    assert DIAGRAM_INSTRUCTION not in system["content"] and CODE_INSTRUCTION not in system["content"]
    assert "mermaid" not in system["content"] and "```python" not in system["content"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert messages[1]["content"].startswith("second question")
    assert len(messages[1]["content"]) == fast_lane.FAST_LANE_TURN_CHARS
    assert messages[-1] == {"role": "user", "content": "hi ??"}
    assert sum(1 for m in messages if m["role"] == "system") == 1


def test_the_non_lane_prompt_is_unchanged():
    from app.identity import identity_line

    history = [
        {"role": "system", "content": FACTS_HEADER + "\n- Prefers short answers"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    expected_system = ASSISTANT = (
        chat_engine.ASSISTANT_SYSTEM + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION + identity_line()
    )
    # --- AS3 intent-capability BEGIN --- the non-lane prompt now ends with the file capability line
    from app.engines.capability import CAPABILITY_SUFFIX

    ASSISTANT = ASSISTANT + CAPABILITY_SUFFIX
    # --- AS3 intent-capability END ---
    messages = chat_engine._messages("hello", history, "assistant", "GROUNDING")
    assert messages[0] == {"role": "system", "content": ASSISTANT + "\n\nGROUNDING"}
    assert messages[1:] == [*history, {"role": "user", "content": "hello"}]
    assert chat_engine._messages("hello", history, "assistant", "GROUNDING", lane="") == messages
    assert expected_system in messages[0]["content"]


def test_the_lane_runs_one_call_of_1024_tokens_with_thinking_off(monkeypatch):
    import asyncio

    calls = []

    async def fake_stream(messages, **kwargs):
        calls.append(kwargs)
        yield ("token", "Hello!")

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    answer = asyncio.run(
        chat_engine.run_chat_engine(
            "hi", [], emit, mode="assistant", model_choice="smart", effort="fast", lane="greeting"
        )
    )
    assert answer == "Hello!"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == fast_lane.FAST_LANE_MAX_TOKENS == 1024
    assert calls[0]["temperature"] == 0.6
    assert calls[0]["effort"] == "fast"
    assert llm.wants_thinking("smart", "fast") is False
    assert events[-1] == ("meta", {"route": "chat"})
