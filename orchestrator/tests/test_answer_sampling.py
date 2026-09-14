"""core/answer_sampling — the per-request sampling and Fast length policy.

Pure: no engine, no network. What is pinned (answer-quality design C2/C9,
2026-09-14):

- the default thinking-off Fast profile is LEGACY: temperature 0.6 and nothing
  else, exactly what engines/chat.py has always sent;
- the Qwen instruct profile is opt-in and carries no presence penalty unless
  1.5 is explicitly enabled, the ask is prose, and no non-Latin letters appear
  in the message or recent history; 1.0 is never accepted;
- the routed thinking profile and the closure profile are the evaluated ones;
- key placement: OpenAI keys top-level, vLLM extensions in extra_body, never a
  seed;
- Fast length caps by shape.
"""
from __future__ import annotations

import importlib
import time

import pytest

from app.core import answer_sampling as S


# ------------------------------------------------------------ defaults --


def test_default_profile_is_legacy_and_sends_temperature_only():
    assert S.SAMPLING_PROFILE == S.PROFILE_LEGACY
    assert S.PROSE_PRESENCE == 0.0
    assert S.thinking_off_sampling(shape=S.SHAPE_PROSE, message="hi") == {"temperature": 0.6}
    assert S.thinking_off_sampling(shape=S.SHAPE_STRUCTURED, message="a table") == {"temperature": 0.6}
    # Legacy ignores the presence flag entirely.
    assert S.thinking_off_sampling(
        shape=S.SHAPE_PROSE, message="hello there", prose_presence=1.5
    ) == {"temperature": 0.6}


def test_the_fast_model_choice_keeps_the_callers_temperature_alone():
    for profile in S.PROFILES:
        assert S.thinking_off_sampling(shape=S.SHAPE_PROSE, model_choice="fast", profile=profile) == {}


def test_qwen_instruct_has_no_presence_penalty_by_default():
    out = S.thinking_off_sampling(shape=S.SHAPE_PROSE, message="tell me about tea", profile=S.PROFILE_QWEN_INSTRUCT)
    assert out == {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0}


def test_presence_penalty_only_for_latin_prose_when_enabled():
    kw = dict(profile=S.PROFILE_QWEN_INSTRUCT, prose_presence=1.5)
    assert S.thinking_off_sampling(shape=S.SHAPE_PROSE, message="tell me about tea", **kw)["presence_penalty"] == 1.5
    # Accented Latin, punctuation, box drawing, arrows and emoji are not a script.
    assert "presence_penalty" in S.thinking_off_sampling(
        shape=S.SHAPE_PROSE, message="café naïve Việt ─┼─ → 🙂 “quotes”", **kw
    )
    for shape in (S.SHAPE_STRUCTURED, S.SHAPE_LONGFORM):
        assert "presence_penalty" not in S.thinking_off_sampling(shape=shape, message="tea", **kw)
    for text in ("नमस्ते, चाय के बारे में बताइए", "ચા વિશે કહો", "お茶について", "Привет"):
        assert "presence_penalty" not in S.thinking_off_sampling(shape=S.SHAPE_PROSE, message=text, **kw)
    # A Latin-script follow-up in a Hindi conversation still answers in Hindi.
    history = [{"role": "user", "content": "मुझे चाय के बारे में बताओ"}, {"role": "assistant", "content": "ज़रूर"}]
    assert "presence_penalty" not in S.thinking_off_sampling(
        shape=S.SHAPE_PROSE, message="aur batao", history=history, **kw
    )


@pytest.mark.parametrize(
    "raw, expected",
    [("", 0.0), ("0", 0.0), ("1.5", 1.5), ("1.50", 1.5), ("1.0", 0.0), ("1", 0.0), ("2", 0.0), ("abc", 0.0), ("nan", 0.0)],
)
def test_presence_env_accepts_only_the_evaluated_value(monkeypatch, raw, expected):
    monkeypatch.setenv("ANSWER_PROSE_PRESENCE_PENALTY", raw)
    assert S._env_presence("ANSWER_PROSE_PRESENCE_PENALTY") == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("", "legacy"), ("legacy", "legacy"), ("QWEN_INSTRUCT", "qwen_instruct"), (" qwen_instruct ", "qwen_instruct"),
     ("qwen_thinking", "legacy"), ("garbage", "legacy")],
)
def test_profile_env_is_parsed_safely(monkeypatch, raw, expected):
    monkeypatch.setenv("ANSWER_SAMPLING_PROFILE", raw)
    assert S._env_choice("ANSWER_SAMPLING_PROFILE", S.PROFILES, S.PROFILE_LEGACY) == expected


def test_env_is_read_once_at_import(monkeypatch):
    monkeypatch.setenv("ANSWER_SAMPLING_PROFILE", "qwen_instruct")
    monkeypatch.setenv("ANSWER_PROSE_PRESENCE_PENALTY", "1.5")
    module = importlib.reload(S)
    try:
        assert module.SAMPLING_PROFILE == "qwen_instruct"
        assert module.thinking_off_sampling(shape="prose", message="tea")["presence_penalty"] == 1.5
    finally:
        monkeypatch.delenv("ANSWER_SAMPLING_PROFILE")
        monkeypatch.delenv("ANSWER_PROSE_PRESENCE_PENALTY")
        importlib.reload(S)
    assert S.SAMPLING_PROFILE == "legacy"


# ------------------------------------------------- thinking and closure --


def test_routed_thinking_profile_is_qwen_thinking_general():
    assert S.routed_thinking_sampling() == {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5,
    }
    # A copy, never the shared constant.
    S.routed_thinking_sampling()["temperature"] = 9
    assert S.THINKING_SAMPLING["temperature"] == 1.0


def test_closure_drops_presence_penalty_for_structured_answers_only():
    assert S.closure_sampling(S.SHAPE_PROSE)["presence_penalty"] == 1.5
    assert S.closure_sampling(S.SHAPE_LONGFORM)["presence_penalty"] == 1.5
    structured = S.closure_sampling(S.SHAPE_STRUCTURED)
    assert "presence_penalty" not in structured
    assert structured == {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0}


# ------------------------------------------------------------ placement --


def test_placement_top_level_and_extra_body():
    request = {"temperature": 0.6, "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    S.place_sampling(request, S.THINKING_SAMPLING, vllm_extensions=True)
    assert request["temperature"] == 1.0
    assert request["top_p"] == 0.95
    assert request["presence_penalty"] == 1.5
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}, "top_k": 20, "min_p": 0.0}
    for key in ("top_k", "min_p"):
        assert key not in request


def test_placement_creates_extra_body_and_drops_extensions_for_strict_backends():
    request: dict = {}
    S.place_sampling(request, {"top_k": 20, "repetition_penalty": 1.05}, vllm_extensions=True)
    assert request == {"extra_body": {"top_k": 20, "repetition_penalty": 1.05}}
    strict: dict = {"temperature": 0.2}
    S.place_sampling(strict, S.THINKING_SAMPLING, vllm_extensions=False)
    assert strict == {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 1.5}


@pytest.mark.parametrize("bad", [{"seed": 1}, {"frequency_penalty": 0.5}, {"top_p": "0.9"}, {"top_k": True}, {"tempreature": 1}])
def test_keys_this_layer_never_sends_are_refused(bad):
    with pytest.raises(ValueError):
        S.place_sampling({}, bad, vllm_extensions=True)
    with pytest.raises(ValueError):
        S.validate_sampling(bad)


def test_the_legacy_profile_placed_changes_nothing_but_temperature():
    request = {"model": "m", "temperature": 0.6, "max_tokens": 8000, "stream": True,
               "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in request.items()}
    S.place_sampling(request, S.thinking_off_sampling(shape="prose"), vllm_extensions=True)
    assert request == before


# ---------------------------------------------------------------- shape --


@pytest.mark.parametrize(
    "message, mode, shape",
    [
        ("hey, how are you?", "assistant", "prose"),
        ("what is the capital of France", "assistant", "prose"),
        ("explain photosynthesis simply", "assistant", "prose"),
        ("give me a markdown table of the planets", "assistant", "structured"),
        ("return it as JSON", "assistant", "structured"),
        ("export this to CSV please", "assistant", "structured"),
        ("draw a mermaid flowchart of login", "assistant", "structured"),
        ("draw a diagram of the pipeline", "assistant", "structured"),
        ("hello", "salesforce", "structured"),
        ("write a python function that parses dates", "assistant", "longform"),
        ("fix this:\n```py\nprint(1\n```", "assistant", "longform"),
        ("give me a detailed report on solar", "assistant", "longform"),
        ("a list of 100 baby names", "assistant", "longform"),
        ("a list of 10 baby names", "assistant", "prose"),
        ("translate this: " + "word " * 500, "assistant", "longform"),
        ("translate hello to Hindi", "assistant", "prose"),
        # word boundaries: 'classic', 'written', 'fully' do not match.
        ("a classic written fully by hand", "assistant", "prose"),
    ],
)
def test_shape_truth_table(message, mode, shape):
    assert S.shape_for(message, mode=mode) == shape


def test_fast_caps():
    assert S.fast_caps("prose") == (8000, 8000)
    assert S.fast_caps("longform") == (8000, 64000)
    assert S.fast_caps("structured") == (8000, 64000)


def test_shape_and_script_scan_are_bounded_on_huge_inputs():
    message = ("abc def " * 150_000) + " नमस्ते"
    history = [{"role": "user", "content": "x" * 2_000_000}] * 50
    started = time.perf_counter()
    for _ in range(20):
        S.shape_for(message)
        S.has_non_latin_letters([message, *S._history_texts(history)])
    per_call_ms = (time.perf_counter() - started) * 1000 / 20
    assert per_call_ms < 10, per_call_ms


# ------------------------------------------------------- fast decision --


def test_fast_decision_only_for_fast_assistant_and_salesforce():
    for effort in ("think", "max"):
        assert S.fast_sampling_for("hi", [], mode="assistant", effort=effort, model_choice="smart") is None
    for mode in ("repo", "sql", "search", "document"):
        assert S.fast_sampling_for("hi", [], mode=mode, effort="fast", model_choice="smart") is None

    prose = S.fast_sampling_for("hi", [], mode="assistant", effort="fast", model_choice="smart")
    assert prose.sampling == {"temperature": 0.6}
    assert prose.enable_thinking is None
    assert (prose.shape, prose.segment_max_tokens, prose.total_max_tokens) == ("prose", 8000, 8000)

    sf = S.fast_sampling_for("list my open opportunities", [], mode="salesforce", effort="fast", model_choice="smart")
    assert (sf.shape, sf.segment_max_tokens, sf.total_max_tokens) == ("structured", 8000, 64000)

    router = S.fast_sampling_for("hi", [], mode="assistant", effort="fast", model_choice="fast")
    assert router.sampling == {} and router.profile == "legacy"


def test_fast_decision_with_the_qwen_profile_forwards_the_gate():
    plan = S.fast_sampling_for(
        "tell me about tea", [], mode="assistant", effort="fast", model_choice="smart",
        profile="qwen_instruct", prose_presence=1.5,
    )
    assert plan.sampling["presence_penalty"] == 1.5 and plan.profile == "qwen_instruct"
    table = S.fast_sampling_for(
        "a table of teas", [], mode="assistant", effort="fast", model_choice="smart",
        profile="qwen_instruct", prose_presence=1.5,
    )
    assert "presence_penalty" not in table.sampling


# ------------------------------------------- verifier fixes (2026-09-15) --


@pytest.mark.parametrize(
    "message, shape",
    [
        ("Explain how TCP congestion control works in depth", "longform"),
        ("Give me a comprehensive guide to learning Kubernetes", "longform"),
        ("Draft a 5000-word short story about a lighthouse keeper", "longform"),
        ("List 100 English idioms with meanings", "longform"),
        ("Give me 80 interview questions for a Salesforce admin", "longform"),
        ("Diwali par 2000 words ka nibandh likho", "longform"),
        ("मानसून पर 3000 शब्दों में विस्तार से निबंध लिखिए।", "longform"),
        ("ગુજરાતીમાં દિવાળી પર 2000 શબ્દોનો નિબંધ લખો.", "longform"),
        ("मानसून पर ३००० शब्द", "longform"),
        # Numbers in an ordinary question are not a size.
        ("How many calories are in 2 eggs and 250 g rice?", "prose"),
        ("What is 15% of 2,340?", "prose"),
        ("I scored 450 marks out of 600, what percentage is that?", "prose"),
        ("give me 5 tips for sleep", "prose"),
        ("likes and dislikes of cats", "prose"),
    ],
)
def test_long_form_asks_the_first_lexicon_missed(message, shape):
    assert S.shape_for(message) == shape


def test_a_non_latin_conversation_is_not_held_to_the_one_call_prose_total():
    """~3.8 tokens a Gujarati word (measured): 8,000 tokens is a third of the
    English length, so dense scripts keep the extended total."""
    for text in ("ચા વિશે કહો", "चाय के बारे में बताइए"):
        plan = S.fast_sampling_for(text, [], mode="assistant", effort="fast", model_choice="smart")
        assert (plan.shape, plan.total_max_tokens) == ("prose", 64000)
    history = [{"role": "user", "content": "ચા વિશે કહો"}]
    plan = S.fast_sampling_for("aur batao", history, mode="assistant", effort="fast", model_choice="smart")
    assert plan.total_max_tokens == 64000
    plan = S.fast_sampling_for("tell me about tea", [], mode="assistant", effort="fast", model_choice="smart")
    assert plan.total_max_tokens == 8000


def test_presence_penalty_is_withheld_when_the_answer_is_in_another_language():
    kw = dict(profile=S.PROFILE_QWEN_INSTRUCT, prose_presence=1.5)
    for text in ("translate hello to Hindi", "how do I say thank you in Gujarati", "reply in japanese please"):
        assert "presence_penalty" not in S.thinking_off_sampling(shape=S.SHAPE_PROSE, message=text, **kw)


@pytest.mark.parametrize(
    "raw, expected",
    [("", 8000), ("8000", 8000), ("16000", 16000), ("64000", 64000), ("8192", 8000), ("12000", 8000),
     ("0", 8000), ("-5", 8000), ("abc", 8000)],
)
def test_prose_total_env_refuses_the_just_above_one_segment_values(monkeypatch, raw, expected):
    monkeypatch.setenv("ANSWER_FAST_PROSE_TOTAL_TOKENS", raw)
    assert S._env_prose_total("ANSWER_FAST_PROSE_TOTAL_TOKENS") == expected


def test_prose_total_env_is_applied_at_import(monkeypatch):
    monkeypatch.setenv("ANSWER_FAST_PROSE_TOTAL_TOKENS", "1000000")
    module = importlib.reload(S)
    try:
        assert module.fast_caps("prose") == (8000, 1000000)
        assert module.fast_caps("longform") == (8000, 1000000)
    finally:
        monkeypatch.delenv("ANSWER_FAST_PROSE_TOTAL_TOKENS")
        importlib.reload(S)
    assert S.fast_caps("prose") == (8000, 8000)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_sampling_values_are_refused(bad):
    with pytest.raises(ValueError):
        S.validate_sampling({"temperature": bad})
