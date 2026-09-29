"""The segmentation rule on its own: observations in, events out.

Each observation is what the decode worker sees after one decode step: the
recognizer's whole text, whether the endpoint rule holds, how many client
samples the stream has been fed, and (when the binding has them) token
positions, already in the client's time base.
"""
from __future__ import annotations

from conftest import server

CHUNK = 2560  # 160 ms at 16 kHz


def make(**overrides) -> "server.Segmenter":
    values = dict(first_sample=0, first_u=0, chunk_samples=CHUNK, endpoint_s=0.6, max_utterance_s=30.0,
                  renew_chars=2000, renew_hold_s=1.5, renew_after_s=1800.0)
    values.update(overrides)
    return server.Segmenter(**values)


def kinds(events):
    return [(e["type"], e.get("text", e.get("active"))) for e in events]


def timing(text, tokens, seconds, pad=0, origin=0):
    """TokenTimes the way the worker builds them: stream seconds, mapped to
    client samples past `pad` samples of inserted silence."""
    return server.TokenTimes.build(text, tokens, seconds, 0.0,
                                   lambda t: origin + max(0, int(round(t * 16000)) - pad))


def test_a_partial_is_the_whole_pending_text_and_only_sent_when_it_changes():
    s = make()
    events, renew = s.observe("hello", False, CHUNK)
    assert kinds(events) == [("speech", True), ("partial", "hello")]
    assert events[1]["u"] == 0 and not renew
    assert s.observe("hello", False, 2 * CHUNK)[0] == []
    events, _ = s.observe("hello world", False, 3 * CHUNK)
    assert kinds(events) == [("partial", "hello world")]


def test_a_final_commits_on_the_endpoint_rising_edge_only():
    s = make()
    s.observe("hello world", False, 4 * CHUNK)
    events, _ = s.observe("hello world", True, 8 * CHUNK)
    assert kinds(events) == [("final", "hello world"), ("speech", False)]
    assert events[0]["u"] == 0 and events[0]["endpoint_ms"] == 600
    # The endpoint staying true is not a new utterance.
    assert s.observe("hello world", True, 9 * CHUNK)[0] == []
    events, _ = s.observe("hello world again", False, 12 * CHUNK)
    assert kinds(events) == [("speech", True), ("partial", "again")]
    assert events[1]["u"] == 1
    events, _ = s.observe("hello world again", True, 16 * CHUNK)
    assert kinds(events) == [("final", "again"), ("speech", False)]
    assert events[0]["u"] == 1


def test_punctuation_alone_is_never_an_utterance_and_the_next_one_does_not_open_with_it():
    s = make()
    s.observe("hello", False, CHUNK)
    s.observe("hello", True, 6 * CHUNK)
    # The model emits the full stop after the endpoint fired.
    assert s.observe("hello.", False, 7 * CHUNK)[0] == []
    assert s.observe("hello.", True, 12 * CHUNK)[0] == []
    events, _ = s.observe("hello. Next", False, 14 * CHUNK)
    assert [e["text"] for e in events if e["type"] == "partial"] == ["Next"]
    events, _ = s.observe("hello. Next", True, 20 * CHUNK)
    assert [e["text"] for e in events if e["type"] == "final"] == ["Next"]
    assert s.finals == 2


def test_an_utterance_drops_the_previous_sentences_punctuation_but_keeps_its_own():
    assert server.utterance_text(", and  I'm told") == "and I'm told"
    assert server.utterance_text("\u0964 लेकिन") == "लेकिन"
    assert server.utterance_text("? Squeak, squeak.") == "Squeak, squeak."
    assert server.utterance_text("\u201cHello,\u201d she said") == "\u201cHello,\u201d she said"
    assert server.utterance_text("\u00bfQu\u00e9?") == "\u00bfQu\u00e9?"


def test_whitespace_runs_from_space_tokens_are_collapsed():
    s = make()
    events, _ = s.observe("is it not boys?  she said", False, CHUNK)
    assert events[-1]["text"] == "is it not boys? she said"


def test_a_long_utterance_is_forced_out_keeping_its_last_three_words_pending():
    s = make(max_utterance_s=1.0)  # 16000 samples
    words = [f"w{i}" for i in range(10)]
    s.observe(" ".join(words[:2]), False, CHUNK)  # first seen here: began one chunk earlier, at 0
    events, _ = s.observe(" ".join(words), False, 16000)
    final = [e for e in events if e["type"] == "final"]
    partial = [e for e in events if e["type"] == "partial"]
    assert [f["text"] for f in final] == [" ".join(words[:7])]
    assert final[0]["endpoint_ms"] == 0 and final[0]["u"] == 0 and final[0]["_kind"] == "final_forced"
    assert [p["text"] for p in partial] == [" ".join(words[7:])] and partial[0]["u"] == 1
    # Speech goes on: no "speech inactive" for a forced final.
    assert not [e for e in events if e["type"] == "speech"]
    # And the held-back words commit normally at the pause.
    events, _ = s.observe(" ".join(words), True, 22000)
    assert [e["text"] for e in events if e["type"] == "final"] == [" ".join(words[7:])]


def test_a_short_utterance_is_never_forced():
    s = make(max_utterance_s=1.0)
    s.observe("one two three", False, CHUNK)
    assert [e for e in s.observe("one two three", False, 64000)[0] if e["type"] == "final"] == []


def test_text_without_spaces_is_forced_out_by_length():
    s = make(max_utterance_s=1.0)
    text = "字" * (server.MAX_PENDING_CHARS + 20)
    s.observe(text[:10], False, CHUNK)
    events, _ = s.observe(text, False, 20000)
    final = [e for e in events if e["type"] == "final"]
    assert len(final) == 1 and len(final[0]["text"]) == len(text) - server.KEEP_CHARS
    assert [e["text"] for e in events if e["type"] == "partial"] == ["字" * server.KEEP_CHARS]


def test_too_much_pending_text_is_forced_out_whatever_its_age():
    # The gateway drops any event over 4,000 characters; a young utterance
    # that already holds more than MAX_PENDING_CHARS goes out anyway.
    s = make()
    words = ["word%03d" % i for i in range(200)]  # 1,599 characters
    s.observe(" ".join(words[:3]), False, CHUNK)
    events, _ = s.observe(" ".join(words), False, 2 * CHUNK)
    final = [e for e in events if e["type"] == "final"]
    assert [f["text"] for f in final] == [" ".join(words[:-3])]
    assert [e["text"] for e in events if e["type"] == "partial"] == [" ".join(words[-3:])]


def test_renewal_waits_for_a_held_silence_and_a_big_enough_text():
    s = make(renew_chars=10, renew_hold_s=1.0)  # hold: 16000 samples
    text = "a long enough sentence"
    s.observe(text, False, CHUNK)
    s.observe(text, True, 10 * CHUNK)  # the final
    assert s.observe(text, True, 10 * CHUNK + 15999)[1] is False
    assert s.observe(text, True, 10 * CHUNK + 16000)[1] is True
    s.after_renew(10 * CHUNK + 16000)
    events, _ = s.observe("fresh", False, 20 * CHUNK)
    assert [(e["type"], e["u"], e["text"]) for e in events if e["type"] == "partial"] == [("partial", 1, "fresh")]
    assert s.renewals == 1


def test_no_renewal_while_words_are_pending_or_before_the_endpoint():
    s = make(renew_chars=5, renew_hold_s=0.5)
    s.observe("words pending here", False, CHUNK)
    for fed in range(2 * CHUNK, 40 * CHUNK, CHUNK):
        assert s.observe("words pending here", False, fed)[1] is False
    # The endpoint without a final (pending has words) is not quiet either.
    s2 = make(renew_chars=5, renew_hold_s=0.5)
    s2._was_endpoint = True  # an endpoint already held: no rising edge, words pending
    for fed in range(CHUNK, 40 * CHUNK, CHUNK):
        assert s2.observe("words pending here", True, fed)[1] is False


def test_a_quiet_session_renews_on_time_so_rule_3_never_latches():
    s = make(renew_chars=10_000, renew_hold_s=1.0, renew_after_s=10.0)
    s.observe("short", False, CHUNK)
    s.observe("short", True, 10 * CHUNK)
    assert s.observe("short", True, 9 * 16000)[1] is False   # held, small, not yet 10 s
    assert s.observe("short", True, 10 * 16000)[1] is True


def test_positions_come_from_token_times_in_the_clients_time_base():
    # 160 ms of lead silence in the stream: the tokens' stream times are
    # 0.16 s later than where the client sent them.
    s = make(first_sample=48000, first_u=7)
    t = timing("hello world", [" hel", "lo", " world"], [0.40, 0.48, 0.96], pad=2560)
    events, _ = s.observe("hello world", False, 50000, t)
    speech, partial = events
    assert partial["u"] == 7
    assert partial["start_sample"] == 48000 + int(0.24 * 16000)
    assert partial["end_sample"] == 48000 + int(0.80 * 16000)
    assert speech == {"type": "speech", "active": True, "sample": partial["start_sample"], "_kind": "speech"}


def test_positions_after_a_renewal_follow_the_seed():
    # A renewed stream starts with the lead pad, then a seed of client audio
    # that began at client sample 100000.
    s = make()
    s.observe("one", False, 90000)
    s.observe("one", True, 99000)
    s.after_renew(119200)
    t = timing("two", [" two"], [0.66], pad=2560, origin=100000)
    events, _ = s.observe("two", False, 125000, t)
    partial = [e for e in events if e["type"] == "partial"][0]
    assert partial["start_sample"] == partial["end_sample"] == 100000 + int(0.50 * 16000)


def test_fallback_positions_without_token_times():
    s = make(first_sample=1000)
    s.observe("hello", False, 4 * CHUNK)  # first seen at 4 chunks: starts one chunk before
    events, _ = s.observe("hello", True, 12 * CHUNK)
    final = events[0]
    assert final["start_sample"] == 1000 + 3 * CHUNK
    assert final["end_sample"] == 1000 + 12 * CHUNK - 9600  # endpoint_s (0.6 s) before the step


def test_positions_never_leave_the_audio_received_or_go_back_before_the_last_final():
    s = make()
    late = timing("late", [" late"], [9.0])  # a time past what was fed
    events, _ = s.observe("late", False, 16000, late)
    partial = events[-1]
    assert 0 <= partial["start_sample"] <= partial["end_sample"] <= 16000
    final = s.observe("late", True, 20000, late)[0][0]
    assert final["end_sample"] == 20000
    early = timing("late again", [" late", " again"], [9.0, 0.1])
    events, _ = s.observe("late again", False, 24000, early)
    again = [e for e in events if e["type"] == "partial"][0]
    assert again["start_sample"] == 20000  # never before the final that closed at 20000


def test_finish_commits_pending_words_and_nothing_else():
    s = make()
    s.observe("almost done", False, CHUNK)
    events = s.finish("almost done", 3 * CHUNK)
    assert kinds(events) == [("final", "almost done"), ("speech", False)]
    assert events[0]["_kind"] == "final_flush" and events[0]["endpoint_ms"] == 0
    assert s.finish("almost done.", 4 * CHUNK) == []


def test_a_shrinking_text_is_clamped_not_sliced():
    s = make()
    s.observe("one two", False, CHUNK)
    s.observe("one two", True, 8 * CHUNK)
    events, _ = s.observe("one", False, 9 * CHUNK)  # cannot happen with sherpa; must not crash
    assert events == []


def test_token_times_line_up_with_the_stripped_text_or_are_refused():
    t = server.TokenTimes.build("Cotton is", [" Co", "tton", " ", "is"], [0.1, 0.2, 0.3, 0.4], 1.0,
                                lambda seconds: int(round(seconds * 10)))
    assert t is not None
    assert t.span(0, 6) == (11, 12)       # "Cotton"
    assert t.span(7, 9) == (14, 14)       # "is": the lone space token carries no time
    assert t.span(6, 7) is None           # only the space
    ident = lambda seconds: 0  # noqa: E731
    assert server.TokenTimes.build("other text", [" Co", "tton"], [0.1, 0.2], 0.0, ident) is None
    assert server.TokenTimes.build("Cotton", [" Co", "tton"], [0.1], 0.0, ident) is None


def test_words_are_letters_or_digits_in_any_script():
    assert server.has_words("नमस्ते")
    assert server.has_words("42")
    assert not server.has_words(" , . ? ")
    assert server.clean_text("  a \n b  ") == "a b"


def test_coalesce_keeps_only_the_newest_hypothesis_and_every_final():
    batch = [
        {"type": "partial", "u": 3, "text": "a", "compute_ms": 5},
        {"type": "speech", "active": True, "sample": 0},
        {"type": "partial", "u": 3, "text": "a b", "compute_ms": 7},
        {"type": "final", "u": 3, "text": "a b c", "compute_ms": 1},
        {"type": "partial", "u": 4, "text": "d", "compute_ms": 2},
        {"type": "partial", "u": 4, "text": "d e", "compute_ms": 3},
        {"type": "pong", "t": 1},
    ]
    out = server.coalesce(batch)
    assert [(e["type"], e.get("text")) for e in out] == [
        ("speech", None), ("final", "a b c"), ("partial", "d e"), ("pong", None)]
    # The dropped partials' decode time is not lost.
    assert sum(e.get("compute_ms", 0) for e in out) == 5 + 7 + 1 + 2 + 3


def test_the_seed_ring_keeps_the_newest_audio_in_order():
    import numpy as np

    ring = server.Ring(5)
    assert len(ring.last(5)) == 0
    ring.write(np.arange(3, dtype=np.float32))
    assert ring.last(5).tolist() == [0, 1, 2]
    ring.write(np.arange(3, 7, dtype=np.float32))
    assert ring.last(5).tolist() == [2, 3, 4, 5, 6]
    assert ring.last(2).tolist() == [5, 6]
    ring.write(np.arange(10, 20, dtype=np.float32))
    assert ring.last(5).tolist() == [15, 16, 17, 18, 19]
