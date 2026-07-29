"""Phase A: budget maths, auto-compaction, and session isolation.

The load-bearing claims:
- a request always reserves ITS OWN output budget, never a global constant;
- output never drops below the thinking model's floor (an empty answer is
  worse than a shorter history), so input is trimmed further instead;
- folding is idempotent via `covers_through`, so the background and
  synchronous paths cannot double-fold or race;
- a fact stated early survives many compactions;
- one session's summary can never enter another session's prompt.
"""
import asyncio

import pytest

from app import compaction, context, db, summarize
from app.config import settings


@pytest.fixture()
def signed_in(tmp_path):
    """A conversation owned by a real user, so summaries can be stored."""
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "conv-1", "chat")
    return uid


def turns(n: int, prefix: str = "m") -> list:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{prefix}{i}"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Budget maths
# ---------------------------------------------------------------------------


def test_usable_budget_reserves_this_requests_output():
    small = compaction.usable_budget(100_000, compaction.output_reservation(2000))
    large = compaction.usable_budget(100_000, compaction.output_reservation(12000))
    assert large < small, "a bigger output reservation must leave less prompt room"
    assert small == 100_000 - 2000 - settings.context_safety_margin


def test_output_never_drops_below_the_thinking_floor():
    # Even when the caller asks for very little, the reservation holds the
    # floor: a thinking model given 200 tokens returns nothing at all.
    assert compaction.output_reservation(200) == settings.min_output_floor
    assert compaction.output_reservation(None) == max(
        settings.min_output_floor, settings.model_max_output
    )
    assert compaction.output_reservation(12000) == 12000


def test_fraction_is_measured_against_usable_not_the_raw_window():
    b = compaction.Budget(window=1000, output_reserved=400, usable=500, used=250)
    assert b.fraction == 0.5


# ---------------------------------------------------------------------------
# Fold boundary + assembly
# ---------------------------------------------------------------------------


def test_fold_boundary_keeps_the_recent_tail():
    boundary = compaction.fold_boundary(50, covers_through=0)
    assert boundary == 50 - settings.keep_recent_turns


def test_fold_boundary_never_goes_backwards():
    """Idempotency: a second pass cannot re-fold what is already folded."""
    assert compaction.fold_boundary(20, covers_through=15) == 15


def test_assemble_order_is_system_summary_retrieved_recent():
    history = [
        {"role": "system", "content": "engine instructions"},
        *turns(6),
    ]
    out = compaction.assemble(history, "SUMMARY TEXT", covers_through=4,
                              retrieved="RETRIEVED TEXT")
    roles = [m["role"] for m in out]
    assert roles[0] == "system" and out[0]["content"] == "engine instructions"
    assert summarize.SUMMARY_HEADER in out[1]["content"]
    assert out[2]["content"] == "RETRIEVED TEXT"
    # Only the un-folded tail survives verbatim.
    assert [m["content"] for m in out[3:]] == ["m4", "m5"]


def test_assemble_without_a_summary_is_the_plain_history():
    history = turns(4)
    assert compaction.assemble(history, None, 0) == history


# ---------------------------------------------------------------------------
# Compaction: idempotent, bounded, non-fatal
# ---------------------------------------------------------------------------


def fake_summarizer(calls: list):
    async def _summarize(existing, folded):
        calls.append({"existing": existing, "folded": [t["content"] for t in folded]})
        merged = existing + " | " if existing else ""
        return merged + " ".join(t["content"] for t in folded)

    return _summarize


def test_compaction_is_idempotent_and_never_double_folds(signed_in, monkeypatch):
    calls = []
    monkeypatch.setattr(summarize, "summarize", fake_summarizer(calls))
    history = turns(30)

    first = asyncio.run(compaction.compact("conv-1", history))
    assert first is not None
    second = asyncio.run(compaction.compact("conv-1", history))
    assert second is None, "re-running on unchanged history must fold nothing"

    # Every folded turn appears exactly once across all summarizer calls.
    folded = [c for call in calls for c in call["folded"]]
    assert len(folded) == len(set(folded))


def test_compaction_only_folds_the_new_turns_on_the_second_pass(
    signed_in, monkeypatch
):
    calls = []
    monkeypatch.setattr(summarize, "summarize", fake_summarizer(calls))
    asyncio.run(compaction.compact("conv-1", turns(30)))
    asyncio.run(compaction.compact("conv-1", turns(40)))
    # The second call summarizes only turns beyond the first boundary — this
    # is what keeps compaction cost constant as a chat grows.
    assert calls[1]["folded"][0] == f"m{30 - settings.keep_recent_turns}"


def test_compaction_never_folds_the_turn_being_answered(signed_in, monkeypatch):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))
    monkeypatch.setattr(settings, "keep_recent_turns", 0)
    history = turns(5)
    result = asyncio.run(compaction.compact("conv-1", history, force=True))
    assert result["covers_through"] <= len(history) - 1


def test_compaction_failure_is_not_fatal(signed_in, monkeypatch):
    async def boom(existing, folded):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(summarize, "summarize", boom)
    assert asyncio.run(compaction.compact("conv-1", turns(30))) is None
    assert db.get_summary("conv-1") is None


def test_summary_is_condensed_when_it_approaches_its_cap(signed_in, monkeypatch):
    async def huge(existing, folded):
        return "x" * (settings.summary_max_tokens * 3 * 2)

    condensed = {"called": False}

    async def _condense(summary):
        condensed["called"] = True
        return "condensed"

    monkeypatch.setattr(summarize, "summarize", huge)
    monkeypatch.setattr(summarize, "condense", _condense)
    asyncio.run(compaction.compact("conv-1", turns(30)))
    assert condensed["called"]
    assert db.get_summary("conv-1")["summary"] == "condensed"


def test_concurrent_compactions_do_not_double_fold(signed_in, monkeypatch):
    """The background and synchronous paths can fire together."""
    calls = []
    monkeypatch.setattr(summarize, "summarize", fake_summarizer(calls))

    async def race():
        history = turns(40)
        return await asyncio.gather(
            compaction.compact("conv-1", history),
            compaction.compact("conv-1", history),
            compaction.compact("conv-1", history),
        )

    results = asyncio.run(race())
    assert sum(1 for r in results if r is not None) == 1, "only one may fold"
    folded = [c for call in calls for c in call["folded"]]
    assert len(folded) == len(set(folded))


# ---------------------------------------------------------------------------
# The headline claim: a fact from turn 3 survives repeated compaction
# ---------------------------------------------------------------------------


def test_a_turn_3_fact_survives_a_200_turn_conversation(signed_in, monkeypatch):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))
    # Deterministic tokenizer: 10 tokens per message, small window.
    async def counter(base_url, model, messages):
        return 10 * len(messages), 4096

    monkeypatch.setattr(context, "count_tokens", counter)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "my deployment codename is ORION-7"},
    ]
    compactions = 0
    boundaries = []
    for i in range(200):
        history.append({"role": "assistant", "content": f"answer {i}"})
        history.append({"role": "user", "content": f"question {i}"})
        result = asyncio.run(compaction.compact("conv-1", history))
        if result:
            compactions += 1
            boundaries.append(result["covers_through"])

    assert compactions > 1, "compaction must fire repeatedly, not once"
    assert boundaries == sorted(boundaries), "covers_through must advance monotonically"

    row = db.get_summary("conv-1")
    assert "ORION-7" in row["summary"], "the early fact must survive in the summary"

    # …and the assembled prompt no longer carries that turn verbatim.
    assembled = compaction.assemble(history, row["summary"], row["covers_through"])
    verbatim = [
        m["content"] for m in assembled if not m["content"].startswith(
            summarize.SUMMARY_HEADER[:20]
        )
    ]
    assert "my deployment codename is ORION-7" not in verbatim
    assert any("ORION-7" in m["content"] for m in assembled)


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_sessions_never_share_a_summary(signed_in, monkeypatch):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))
    db.create_conversation(signed_in, "conv-2", "other chat")

    asyncio.run(compaction.compact("conv-1", turns(30, prefix="SECRET-A-")))
    asyncio.run(compaction.compact("conv-2", turns(30, prefix="SECRET-B-")))

    a = db.get_summary("conv-1")["summary"]
    b = db.get_summary("conv-2")["summary"]
    assert "SECRET-A-" in a and "SECRET-B-" not in a
    assert "SECRET-B-" in b and "SECRET-A-" not in b

    # And the assembled prompt for one session contains nothing from the other.
    prompt = compaction.assemble(turns(30, prefix="SECRET-A-"), a, 22)
    joined = " ".join(m["content"] for m in prompt)
    assert "SECRET-B-" not in joined


def test_truncating_a_thread_clears_its_summary(signed_in, monkeypatch):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))
    asyncio.run(compaction.compact("conv-1", turns(30)))
    assert db.get_summary("conv-1") is not None
    db.clear_summary("conv-1")
    assert db.get_summary("conv-1") is None


def test_background_compaction_reports_itself_on_the_next_reply(
    signed_in, monkeypatch
):
    """It finishes after `done`, so its notice rides on the following turn."""
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))

    # 4096 window → 2048 reserved → ~1536 usable; 60 turns x 100 tokens is
    # comfortably past the 0.70 background threshold.
    async def counter(base_url, model, messages):
        return 100 * len(messages), 4096

    monkeypatch.setattr(context, "count_tokens", counter)

    async def scenario():
        history = turns(60)
        result = await compaction.maybe_background_compact(
            "conv-1", history, base_url="http://x/v1", model="m"
        )
        assert result is not None, "should have compacted above the bg threshold"
        # Nothing was emitted at the time; the notice is waiting.
        _, info = await compaction.prepare(
            "conv-1", history, "next question",
            base_url="http://x/v1", model="m",
        )
        return info

    info = asyncio.run(scenario())
    assert info["compacted"]["background"] is True
    assert info["compacted"]["folded_turns"] > 0
    # …and it is delivered only once.
    assert compaction.take_pending_notice("conv-1") is None


def test_background_compaction_does_not_fire_below_its_threshold(
    signed_in, monkeypatch
):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))

    async def tiny(base_url, model, messages):
        return 5, 131072

    monkeypatch.setattr(context, "count_tokens", tiny)
    result = asyncio.run(
        compaction.maybe_background_compact(
            "conv-1", turns(40), base_url="http://x/v1", model="m"
        )
    )
    assert result is None


def test_reservation_is_bounded_by_a_small_window():
    """The 8192-token "fast" model must not reserve its entire window."""
    small = compaction.output_reservation(None, window=8192)
    assert small <= 8192 // 2
    assert compaction.usable_budget(8192, small) > 1000, (
        "a small-window model still needs real prompt room"
    )
    # A large window is unaffected.
    assert compaction.output_reservation(None, window=131072) == max(
        settings.min_output_floor, settings.model_max_output
    )


def test_reservation_floor_still_wins_on_a_tiny_window():
    assert compaction.output_reservation(None, window=1000) == settings.min_output_floor


# ---------------------------------------------------------------------------
# The two paths racing on ONE conversation (background is now a detached task)
# ---------------------------------------------------------------------------


def test_background_and_synchronous_compaction_cannot_double_fold(
    signed_in, monkeypatch
):
    """Fire both paths at the same instant on the same conversation.

    Since the background compaction became a detached asyncio task it can
    genuinely overlap the next request's synchronous pass. The guarantees:
    covers_through advances exactly once, every turn is folded exactly once,
    and the summary is written exactly once.
    """
    folded_batches = []
    writes = []

    async def _summarize(existing, folded):
        folded_batches.append([t["content"] for t in folded])
        # Yield inside the summarizer so the two paths genuinely interleave.
        await asyncio.sleep(0.02)
        return (existing + " | " if existing else "") + " ".join(
            t["content"] for t in folded
        )

    real_save = db.save_summary

    def spy_save(conversation_id, summary, covers_through, token_estimate):
        writes.append(covers_through)
        return real_save(conversation_id, summary, covers_through, token_estimate)

    monkeypatch.setattr(summarize, "summarize", _summarize)
    monkeypatch.setattr(db, "save_summary", spy_save)

    async def counter(base_url, model, messages):
        return 100 * len(messages), 4096  # well past both thresholds

    monkeypatch.setattr(context, "count_tokens", counter)

    history = turns(40)

    async def race():
        return await asyncio.gather(
            compaction.maybe_background_compact(
                "conv-1", history, base_url="http://x/v1", model="m"
            ),
            compaction.prepare(
                "conv-1", history, "the new question",
                base_url="http://x/v1", model="m",
            ),
        )

    asyncio.run(race())

    # Exactly one fold happened.
    assert len(writes) == 1, f"summary written {len(writes)} times: {writes}"
    row = db.get_summary("conv-1")
    assert row["covers_through"] == writes[0]

    # Every folded turn appears exactly once across all summarizer calls.
    all_folded = [c for batch in folded_batches for c in batch]
    assert len(all_folded) == len(set(all_folded)), "a turn was folded twice"
    assert len(all_folded) == row["covers_through"]


def test_racing_compactions_leave_a_consistent_boundary(signed_in, monkeypatch):
    """covers_through must never exceed the turns that were actually folded."""
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))

    async def counter(base_url, model, messages):
        return 100 * len(messages), 4096

    monkeypatch.setattr(context, "count_tokens", counter)

    async def race():
        history = turns(50)
        await asyncio.gather(
            *[
                compaction.maybe_background_compact(
                    "conv-1", history, base_url="http://x/v1", model="m"
                )
                for _ in range(3)
            ],
            *[
                compaction.compact("conv-1", history)
                for _ in range(3)
            ],
        )
        return history

    history = asyncio.run(race())
    row = db.get_summary("conv-1")
    _, turn_list = compaction.split_history(history)
    assert 0 < row["covers_through"] <= len(turn_list)


# ---------------------------------------------------------------------------
# Recall recovers a fact the SUMMARIZER deliberately omitted
# ---------------------------------------------------------------------------


def test_a_fact_the_summarizer_omits_is_recovered_by_retrieval(
    signed_in, monkeypatch
):
    """The summary is not the only memory — this is what Phase B is for."""
    from app import llm, recall

    FACT = "the deployment codename is ORION-7"

    async def forgetful(existing, folded):
        # Deliberately drops the planted fact, keeping everything else.
        kept = [t["content"] for t in folded if "ORION-7" not in t["content"]]
        return (existing + " | " if existing else "") + " ".join(kept)

    async def embed(texts, **kwargs):
        # A real embedder puts "what is the codename?" near "the codename is
        # ORION-7"; this fake mirrors that by keying on either word.
        return [
            [1.0, 0.0]
            if ("orion" in t.lower() or "codename" in t.lower())
            else [0.0, 1.0]
            for t in texts
        ]

    monkeypatch.setattr(summarize, "summarize", forgetful)
    monkeypatch.setattr(llm, "embed_texts", embed)

    history = [
        {"role": "user", "content": FACT + " and the cutover is 14 March"},
        {"role": "assistant", "content": "Noted."},
        *turns(30, prefix="filler"),
    ]
    result = asyncio.run(compaction.compact("conv-1", history))
    assert result is not None

    summary = db.get_summary("conv-1")["summary"]
    assert "ORION-7" not in summary, "the summarizer was supposed to drop it"

    # It is also gone from the verbatim tail.
    assembled = compaction.assemble(history, summary, result["covers_through"])
    verbatim = " ".join(
        m["content"] for m in assembled if summarize.SUMMARY_HEADER not in m["content"]
    )
    assert "ORION-7" not in verbatim

    # …but retrieval brings it back, inside the LABELLED block.
    block = asyncio.run(recall.retrieve_block("conv-1", "what is the codename?"))
    assert block is not None
    assert block.startswith(recall.RECALL_HEADER)
    assert "ORION-7" in block

    # And the assembled prompt then carries it in that block, nowhere else.
    with_recall = compaction.assemble(
        history, summary, result["covers_through"], retrieved=block
    )
    carrier = [m for m in with_recall if "ORION-7" in m["content"]]
    assert len(carrier) == 1
    assert carrier[0]["role"] == "system"
    assert recall.RECALL_HEADER in carrier[0]["content"]


# ---------------------------------------------------------------------------
# Adaptive keep-recent
# ---------------------------------------------------------------------------


def test_keeps_fewer_turns_when_the_tail_alone_will_not_fit(
    signed_in, monkeypatch
):
    """A few huge recent turns must not force the clip path."""
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))

    # Each message costs 300 tokens; a 4096 window leaves ~1536 usable, so
    # even 8 recent turns (2400) overflow — 4 or fewer fit.
    async def counter(base_url, model, messages):
        return 300 * len(messages), 4096

    monkeypatch.setattr(context, "count_tokens", counter)

    async def run():
        return await compaction.prepare(
            "conv-1", turns(30), "next question",
            base_url="http://x/v1", model="m",
        )

    history_out, info = asyncio.run(run())
    row = db.get_summary("conv-1")
    _, tail = compaction.split_history(history_out)
    assert row["covers_through"] > 30 - settings.keep_recent_turns, (
        "it should have folded MORE than the default keep window"
    )
    assert len(tail) <= settings.keep_recent_turns
    assert info["summarized_turns"] == row["covers_through"]


def test_adaptive_shrink_never_drops_below_the_minimum_kept(
    signed_in, monkeypatch
):
    monkeypatch.setattr(summarize, "summarize", fake_summarizer([]))

    async def counter(base_url, model, messages):
        return 5000 * len(messages), 4096  # nothing will ever fit

    monkeypatch.setattr(context, "count_tokens", counter)

    async def run():
        return await compaction.prepare(
            "conv-1", turns(30), "q", base_url="http://x/v1", model="m"
        )

    history_out, _ = asyncio.run(run())
    _, tail = compaction.split_history(history_out)
    assert len(tail) >= 1, "the turn being answered must survive"
    assert db.get_summary("conv-1")["covers_through"] <= 30 - compaction.MIN_KEEP_RECENT
