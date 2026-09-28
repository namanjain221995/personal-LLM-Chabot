"""A small spread is a ranking; only a flat vector is a failure.

MEASURED 2026-09-28. Of 29 Fast turns whose pre-pass went to the network,
29 came back `degraded="rerank_degenerate"` — and the live reranker
(Qwen/Qwen3-Reranker-0.6B) was healthy throughout. Probed directly on the
same box:

  mixed pool, "What is the capital city of France?" over 8 documents
      0.999451, 0.996944, 0.003628, 0.000319, 0.000050, 0.000011,
      0.000008, 0.000008      spread 0.999443, order exactly right

  the pool a Fast lookup actually builds — two fetched pages about the ONE
  topic that was asked, chunked into 8 on-topic passages
      0.999973 … 0.999759     spread 0.000214, strict sensible order

The old rule was `spread < 0.02 → not a judgement`, so the second case was
thrown away. Discarding it is not neutral: `web_memory._answerability`
returns before setting `ev.answer`, so the cross-encoder's verdict is lost
AND `Prepared.confidence` stays 0.0, which is what the local-first decision
reads (ADR-0001 D6).

The question is not how wide the spread is. It is whether there is an
ordering at all.
"""
from __future__ import annotations

import pytest

from app import rerank

#: Scores read off the live reranker on 2026-09-28, not invented for a test.
LIVE_MIXED = [0.999451, 0.996944, 0.003628, 0.000319, 0.000050, 0.000011, 0.000008, 0.000008]
LIVE_ON_TOPIC = [0.999973, 0.999973, 0.999952, 0.999948, 0.999879, 0.999873, 0.999867, 0.999759]


def test_a_mixed_pool_is_a_judgement():
    assert rerank.degenerate(LIVE_MIXED) is False


def test_the_pool_a_fast_lookup_actually_builds_is_a_judgement():
    """THE REGRESSION THIS FILE EXISTS FOR. Every candidate is on-topic
    because the fetch went and got pages about the asked topic; the model
    separates them by a fraction of a percent and that is the best
    information anyone has about them."""
    assert max(LIVE_ON_TOPIC) - min(LIVE_ON_TOPIC) < rerank.DEGENERATE_BAND, (
        "premise: this pool is inside the band that used to condemn it"
    )
    assert rerank.degenerate(LIVE_ON_TOPIC) is False


@pytest.mark.parametrize("scores", [
    [0.5] * 8,                      # the model returned a constant
    [0.0] * 8,                      # …or nothing at all
    [1.0] * 8,
])
def test_a_flat_vector_is_still_a_failure(scores):
    assert rerank.degenerate(scores) is True


def test_differences_that_are_only_float_noise_are_still_a_failure():
    noise = [0.5, 0.5 + 1e-9, 0.5 + 2e-9, 0.5 + 3e-9, 0.5 + 1e-9, 0.5, 0.5 + 2e-9, 0.5]
    assert max(noise) - min(noise) < rerank.DEGENERATE_NOISE
    assert rerank.degenerate(noise) is True


def test_a_pool_too_small_to_judge_is_never_condemned():
    """Unchanged: under DEGENERATE_MIN_N there is not enough to call."""
    assert rerank.degenerate([0.5] * (rerank.DEGENERATE_MIN_N - 1)) is False


def test_the_noise_floor_sits_far_below_the_measured_ordering():
    """The two numbers this fix balances, pinned so neither drifts into the
    other: the measured on-topic spread is three orders of magnitude above
    the noise floor, and two below the band."""
    spread = max(LIVE_ON_TOPIC) - min(LIVE_ON_TOPIC)
    assert rerank.DEGENERATE_NOISE < spread < rerank.DEGENERATE_BAND
    assert spread / rerank.DEGENERATE_NOISE > 100


def test_a_wide_spread_is_never_degenerate_whatever_the_floor_is():
    assert rerank.degenerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2]) is False
