"""`wait:cross_chat` is the one context read a Fast turn actually waits for.

Eleven of the twelve `_ContextReads` collect in 0.00-0.02 ms — the read-ahead
bundle works. Cross-chat recall is the exception: 8.2 ms on an empty
conversation, 6.9 ms with documents, 51.9 ms p50 on a compacted 40-turn
thread (waterfall, 2026-09-22, idle-gated).

WHAT THESE TESTS PIN, and nothing more. Inside `semantic_hits` the candidate load (a
fingerprint SELECT, then cached rows or a fetch) and the query embedding do
not depend on each other, and they ran one after the other.

Running them side by side moves work on the normal path. It does NOT "never
create any": three paths start an embedding HEAD would never have started (the
fingerprint invalidates the cache entry and the refetch returns nothing, the
load raises, or the turn is cancelled mid-load), and the honest version of the
design is that every one of them is counted, one reason each, in the counter
that is the kill switch. The first version of this branch counted only the
first of the three and said in the code that it was the only one; the tests
below now pin all three.

THE WHOLE PATH IS OFF BY DEFAULT (`CROSS_CHAT_SPECULATIVE_EMBED`, false),
because what it saves is the wait it overlaps — the whole awaited candidate
load — and nothing else, and against a PostgreSQL on this host, production's
topology, that wait is 1.21-1.32 ms: the saving measures 1.13-1.35 ms p50 over
five alternating passes (2026-09-27), against the 5 ms bar the change had to
clear. An earlier
harness read this as 0.01 ms because its embedding double was
`await asyncio.sleep(27 ms)`, and an asyncio timer is armed through epoll,
whose timeout rounds up to a whole millisecond; the numbers and the direct
measurement of that are beside the flag in app/memory_semantic.py.
The fixtures below turn it ON, because that is the behaviour these tests exist
to pin; one test pins the default itself and proves the default is HEAD's
order.

What this file asserts:

  * the embedding starts early ONLY when this module's own in-process
    candidate cache already holds a non-empty, unexpired entry for the key —
    because `semantic_hits` returns BEFORE it embeds when the candidate list
    is empty, so a new or single-conversation account pays no embedding on
    HEAD and must pay none here. The embedding sidecar is shared with every
    other user of the box and this programme does not speculate on a shared
    scarce resource;
  * the ranking and the answer index stay SERIAL: running them side by side
    is a measured wash (the commit message has the three paired passes), and
    it would hold a second slot of a bounded thread pool;
  * a cold cache keeps HEAD's order exactly, and an empty candidate
    list issues ZERO embeddings and leaves ZERO in flight;
  * the branch never makes more `llm.embed_query` calls than HEAD for the
    same input — one for a whitespace-free question, TWO for a question with
    a newline, because `recall._embed_question` deliberately embeds the RAW
    text while cross-chat embeds the collapsed text. That divergence is not
    closed here: recall's vector, and so recall's ranking, must stay the one
    it computed before;
  * the ordered hits and every score are identical with the overlap on and
    off, over an account with more than 200 candidate rows;
  * every early return and every failure path cancels and AWAITS the sibling
    task, so nothing is left running against a bounded thread pool in front
    of a Postgres server that has run out of connection slots before, and a
    stalled sidecar does not leave EMBED_MAX_INFLIGHT slots held.

SYNTHETIC DATA ONLY: every account, message and question below is invented.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import db, llm, memory_semantic, metrics, recall
from app.config import settings

from tests.test_memory_semantic import _MAPPING, fake_embedder

#: A question with nothing to collapse: `recall`'s raw vector and cross-chat's
#: collapsed vector are the same key, so the turn embeds once.
PLAIN_QUESTION = "who runs the company?"
#: The reviewers' dangerous input. `" ".join(text.split())` changes it, so
#: recall (normalise=False) and cross-chat (the default) hold DIFFERENT LRU
#: keys and the turn embeds TWICE — on HEAD and here alike.
MULTILINE_QUESTION = "who runs the company?\n\nalso  remind me of the ceo"
#: Asks about the earlier conversation itself, so `cross_chat_block` passes
#: pair_answers=True and `_answer_index` runs.
PAST_QUESTION = "what did we decide last time we talked about the company?"


# ---------------------------------------------------------------------------
# instrumentation
# ---------------------------------------------------------------------------
class Stamps:
    """Start/end of every leg, on one process-wide clock (`perf_counter` is
    the same clock in a worker thread as on the loop)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.legs: dict = {}

    def add(self, leg: str, start: float, end: float) -> None:
        with self.lock:
            self.legs.setdefault(leg, []).append((start, end))

    def first(self, leg: str):
        return self.legs[leg][0]

    def count(self, leg: str) -> int:
        return len(self.legs.get(leg, ()))

    def overlap_ms(self, a: str, b: str) -> float:
        (a0, a1), (b0, b1) = self.first(a), self.first(b)
        return (min(a1, b1) - max(a0, b0)) * 1000.0


#: module attribute -> the leg name used in the assertions below
_LEGS = {
    "_load_candidates": "load",
    "_rank_candidates": "rank",
    "_answer_index": "answers",
}


def stamp_legs(monkeypatch, stamps: Stamps, *, load_delay: float = 0.04,
               rank_delay: float = 0.0, answer_delay: float = 0.0) -> None:
    """Time `_load_candidates`, `_rank_candidates` and `_answer_index` without
    replacing any of them: the real function still runs, so the real candidate
    cache is still filled and the real ranking is still computed.

    Each leg can be given a delay. Without one the pure-CPU legs finish in
    microseconds, which says nothing about whether they ran side by side — a
    thread that starts and finishes before its sibling is dispatched looks
    exactly like a serial pair.
    """
    delays = {"_load_candidates": load_delay, "_rank_candidates": rank_delay,
              "_answer_index": answer_delay}
    for name, leg in _LEGS.items():
        real = getattr(memory_semantic, name)

        def wrapper(*a, _real=real, _leg=leg, _delay=delays[name], **k):
            start = time.perf_counter()
            if _delay:
                time.sleep(_delay)
            try:
                return _real(*a, **k)
            finally:
                stamps.add(_leg, start, time.perf_counter())

        monkeypatch.setattr(memory_semantic, name, wrapper)


def stamp_embed_query(monkeypatch, stamps: Stamps, *, delay: float = 0.04,
                      vector=(1.0, 0.0, 0.0), raises=None) -> None:
    """A query-embedding double that takes real time, so an overlap is an
    overlap and not a scheduling artefact."""

    async def embed_query(text, **kw):
        start = time.perf_counter()
        try:
            await asyncio.sleep(delay)
            if raises is not None:
                raise raises
            return list(vector)
        finally:
            stamps.add("embed", start, time.perf_counter())

    monkeypatch.setattr(llm, "embed_query", embed_query)


def dropped() -> dict:
    return {
        dict(key)["reason"]: value
        for key, value in (metrics._counters.get("recall_block_dropped_total") or {}).items()
    }


def wasted() -> float:
    counter = metrics._counters.get("cross_chat_speculative_embed_wasted_total") or {}
    return sum(counter.values())


def wasted_by_reason() -> dict:
    """The kill-switch counter split by reason, so a test can say WHICH case it
    caught. An unregistered reason would arrive here as "other" (metrics.py
    closes the label), which is itself worth failing on."""
    return {
        dict(key).get("reason"): value
        for key, value in (metrics._counters.get("cross_chat_speculative_embed_wasted_total")
                           or {}).items()
    }


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@pytest.fixture()
def account(monkeypatch):
    """One person with two earlier conversations, so a third conversation has
    candidates to recall from."""
    monkeypatch.setattr(llm, "embed_texts", fake_embedder(_MAPPING))
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", True)
    monkeypatch.setattr(settings, "embed_model", "test-embedder")
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 60.0, raising=False)
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
    # Production runs with this OFF (see the module docstring). Turned on here
    # because these tests exist to pin what it does when it is on;
    # test_the_speculation_is_off_by_default pins the default.
    monkeypatch.setattr(settings, "cross_chat_speculative_embed", True, raising=False)
    memory_semantic.invalidate_message_embeddings()
    metrics.reset()

    uid = db.create_user("dana", "hash")
    db.create_conversation(uid, "c1", "Leadership")
    db.add_message(uid, "c1", "user", "Who is the CEO of the company these days")
    db.add_message(uid, "c1", "assistant", "The CEO of the company is Sahil Patel.")
    db.create_conversation(uid, "c2", "Cooking")
    db.add_message(uid, "c2", "user", "How do I bake sourdough bread at home?")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(uid)) == 3
    yield uid
    memory_semantic.invalidate_message_embeddings()


@pytest.fixture()
def lonely_account(monkeypatch):
    """One person, ONE conversation. Excluding it leaves no candidates at all
    — the shape on which HEAD spends no embedding."""
    monkeypatch.setattr(llm, "embed_texts", fake_embedder(_MAPPING))
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", True)
    monkeypatch.setattr(settings, "embed_model", "test-embedder")
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 60.0, raising=False)
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
    # Production runs with this OFF (see the module docstring). Turned on here
    # because these tests exist to pin what it does when it is on;
    # test_the_speculation_is_off_by_default pins the default.
    monkeypatch.setattr(settings, "cross_chat_speculative_embed", True, raising=False)
    memory_semantic.invalidate_message_embeddings()
    metrics.reset()

    uid = db.create_user("evan", "hash")
    db.create_conversation(uid, "only", "The only chat")
    db.add_message(uid, "only", "user", "Who is the CEO of the company these days")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(uid)) == 1
    yield uid
    memory_semantic.invalidate_message_embeddings()


@pytest.fixture()
def big_account(monkeypatch):
    """More than 200 candidate rows, which is where the ranking cost lives."""
    monkeypatch.setattr(llm, "embed_texts", fake_embedder(_MAPPING))
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", True)
    monkeypatch.setattr(settings, "embed_model", "test-embedder")
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 60.0, raising=False)
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
    # Production runs with this OFF (see the module docstring). Turned on here
    # because these tests exist to pin what it does when it is on;
    # test_the_speculation_is_off_by_default pins the default.
    monkeypatch.setattr(settings, "cross_chat_speculative_embed", True, raising=False)
    memory_semantic.invalidate_message_embeddings()
    metrics.reset()

    uid = db.create_user("frida", "hash")
    topics = ["the company board", "sourdough starter hydration", "ceo succession"]
    for c in range(6):
        conv = f"big{c}"
        db.create_conversation(uid, conv, f"Working chat {c}")
        for i in range(38):
            topic = topics[(c + i) % len(topics)]
            db.add_message(uid, conv, "user",
                           f"Question {c}.{i}: what should I know about {topic}?")
            db.add_message(uid, conv, "assistant",
                           f"Answer {c}.{i}: here is what matters about {topic}.")
    embedded = 0
    for _ in range(12):
        drained = asyncio.run(memory_semantic.ensure_message_embeddings(uid))
        embedded += drained
        if drained == 0:
            break
    assert embedded > 200, embedded
    yield uid
    memory_semantic.invalidate_message_embeddings()


def hits(uid, question=PLAIN_QUESTION, exclude="new-conv", **kw):
    return asyncio.run(memory_semantic.semantic_hits(uid, question, exclude, **kw))


async def warm_key(uid, exclude: str, question: str = PLAIN_QUESTION) -> None:
    """One turn, so the candidate cache holds this key's rows — the state a
    SECOND turn of the same conversation starts from."""
    await memory_semantic.semantic_hits(uid, question, exclude)


# ---------------------------------------------------------------------------
# (1) the warm-cache turn overlaps the candidate load and the embedding
# ---------------------------------------------------------------------------
def test_a_warm_candidate_cache_runs_the_embedding_beside_the_candidate_load(account, monkeypatch):
    stamps = Stamps()

    async def scenario():
        await warm_key(account, "new-conv")
        stamp_legs(monkeypatch, stamps, load_delay=0.04)
        stamp_embed_query(monkeypatch, stamps, delay=0.04)
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    found = asyncio.run(scenario())

    assert stamps.count("load") == 1 and stamps.count("embed") == 1
    load_start, load_end = stamps.first("load")
    embed_start, _embed_end = stamps.first("embed")
    # FAILS ON HEAD: HEAD awaits the load to completion, then embeds.
    assert embed_start < load_end, (
        f"the embedding started {(embed_start - load_end) * 1000:.1f} ms AFTER the "
        "candidate load finished; the two legs are still serial"
    )
    # (`load_start` is stamped inside the worker thread, so it can be a
    # fraction of a millisecond later than the embedding's first await.)
    assert stamps.overlap_ms("load", "embed") > 10.0
    assert found and "Sahil Patel" in found[0]["snippet"]
    assert wasted() == 0


# ---------------------------------------------------------------------------
# (2) a COLD cache keeps HEAD's order, and an empty account embeds nothing
# ---------------------------------------------------------------------------
def test_a_cold_candidate_cache_keeps_the_embedding_behind_the_load(account, monkeypatch):
    stamps = Stamps()
    stamp_legs(monkeypatch, stamps, load_delay=0.04)
    stamp_embed_query(monkeypatch, stamps, delay=0.04)

    found = asyncio.run(memory_semantic.semantic_hits(account, PLAIN_QUESTION, "cold-conv"))

    _load_start, load_end = stamps.first("load")
    embed_start, _embed_end = stamps.first("embed")
    assert embed_start >= load_end, (
        "a cold cache must not speculate: the account might have no candidates "
        "at all, and HEAD would then embed nothing"
    )
    assert found
    assert wasted() == 0


def test_an_empty_candidate_list_issues_no_embedding_and_leaves_nothing_in_flight(
    lonely_account, monkeypatch
):
    stamps = Stamps()
    stamp_legs(monkeypatch, stamps, load_delay=0.0)
    stamp_embed_query(monkeypatch, stamps, delay=0.0)

    found = asyncio.run(memory_semantic.semantic_hits(lonely_account, PLAIN_QUESTION, "only"))

    assert found == []
    assert stamps.count("embed") == 0, "HEAD spends no embedding here, so neither may this"
    assert llm._EMBED_INFLIGHT == {}
    assert wasted() == 0


def test_a_warm_cache_that_the_fingerprint_invalidates_to_empty_is_counted_and_cancelled(
    account, monkeypatch
):
    """The one residual case: the cache said this key had rows, the refetch
    came back with none. The embedding is cancelled, nothing is left in
    flight, and the waste is counted — if that counter is ever non-zero over a
    corpus the speculation is dropped."""
    stamps = Stamps()

    async def scenario():
        await warm_key(account, "new-conv")
        stamp_embed_query(monkeypatch, stamps, delay=0.05)
        monkeypatch.setattr(memory_semantic, "_load_candidates", lambda *a, **k: [])
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    assert asyncio.run(scenario()) == []
    assert wasted() == 1
    assert llm._EMBED_INFLIGHT == {}


# ---------------------------------------------------------------------------
# (3) never more `embed_query` calls than HEAD, for the same input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question, expected_sidecar_calls",
    [(PLAIN_QUESTION, 1), (MULTILINE_QUESTION, 2)],
    ids=["whitespace-free-question", "question-with-a-newline"],
)
def test_the_turn_embeds_no_more_often_than_head(account, monkeypatch, question,
                                                 expected_sidecar_calls):
    """One Fast turn runs in-conversation recall and cross-chat recall
    concurrently, exactly as context assembly does, with the REAL `embed_query`
    (its LRU and its single-flight map) over a faked sidecar.

    A whitespace-free question is one sidecar round trip for both readers. A
    question with a newline is TWO, on HEAD and here alike: `recall` embeds the
    raw text on purpose. Closing that would change recall's vector, its chunk
    ranking and therefore an answer, so it stays open.
    """
    monkeypatch.setattr(settings, "semantic_recall_enabled", True)
    conv = "live-conv"
    db.create_conversation(account, conv, "The current chat")

    sidecar = {"n": 0}

    async def embed_texts(texts, **kw):
        sidecar["n"] += 1
        return [[1.0, 0.0, 0.0] for _ in texts]

    query_calls = {"n": 0}
    real_embed_query = llm.embed_query

    async def counting_embed_query(text, **kw):
        query_calls["n"] += 1
        return await real_embed_query(text, **kw)

    async def one_turn():
        llm.embed_cache_clear()
        sidecar["n"] = 0
        query_calls["n"] = 0
        await asyncio.gather(
            recall.retrieve_block(conv, question, effort="fast"),
            memory_semantic.cross_chat_block(account, question, conv),
        )
        return query_calls["n"], sidecar["n"]

    async def scenario():
        monkeypatch.setattr(llm, "embed_texts", embed_texts)
        await recall.index_folded(
            conv, [{"role": "user", "content": "the company ledger closes on Friday"}], 0
        )
        monkeypatch.setattr(llm, "embed_query", counting_embed_query)
        with pytest.MonkeyPatch.context() as patch:
            # HEAD's order, through the production switch rather than a
            # stand-in for it: with the flag off the gate is never consulted,
            # so the candidate load is awaited before anything is embedded.
            patch.setattr(settings, "cross_chat_speculative_embed", False, raising=False)
            memory_semantic.invalidate_message_embeddings()
            await one_turn()                   # warm the candidate cache
            head = await one_turn()
        memory_semantic.invalidate_message_embeddings()
        await one_turn()                       # warm the candidate cache again
        return head, await one_turn()

    head, branch = asyncio.run(scenario())
    assert branch[0] <= head[0], f"branch made {branch[0]} embed_query calls, HEAD {head[0]}"
    assert branch[1] <= head[1], f"branch made {branch[1]} sidecar calls, HEAD {head[1]}"
    assert head[1] == expected_sidecar_calls
    assert wasted() == 0


# ---------------------------------------------------------------------------
# (4) the answer invariant, over more than 200 candidate rows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("question", [PLAIN_QUESTION, MULTILINE_QUESTION, PAST_QUESTION])
def test_more_than_two_hundred_rows_rank_identically_with_the_overlap_on_and_off(
    big_account, monkeypatch, question
):
    ranked: dict = {}
    real_rank = memory_semantic._rank_candidates

    def capture(arm):
        def wrapper(q, vec, candidates):
            out = real_rank(q, vec, candidates)
            ranked.setdefault(arm, []).append([(s, c["message_id"]) for s, c in out])
            return out
        return wrapper

    async def arm(name, speculate):
        """One arm. Its patches live in their OWN context so undoing them
        cannot reach the fixture's, which the next arm still needs."""
        memory_semantic.invalidate_message_embeddings()
        llm.embed_cache_clear()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(memory_semantic, "_rank_candidates", capture(name))
            if not speculate:
                # HEAD's order exactly, and through the production switch: with
                # the flag off the candidate load is awaited to completion
                # before anything is embedded.
                patch.setattr(settings, "cross_chat_speculative_embed", False,
                              raising=False)
            await memory_semantic.semantic_hits(big_account, question, "new-conv")  # warm
            out = await memory_semantic.semantic_hits(big_account, question, "new-conv")
            block = await memory_semantic.cross_chat_block(big_account, question, "new-conv")
        return out, block

    async def scenario():
        head = await arm("head", speculate=False)
        branch = await arm("branch", speculate=True)
        return head, branch

    (head_hits, head_block), (branch_hits, branch_block) = asyncio.run(scenario())

    assert ranked["branch"] == ranked["head"], "the (score, message_id) order moved"
    assert branch_hits == head_hits
    assert branch_block == head_block
    assert [h["score"] for h in branch_hits] == [h["score"] for h in head_hits]
    assert wasted() == 0


# ---------------------------------------------------------------------------
# (5) EmbedUnavailable, on every path it can arrive on
# ---------------------------------------------------------------------------
def test_an_unavailable_embedding_gives_head_s_result_on_the_serial_path(account, monkeypatch):
    stamps = Stamps()
    stamp_embed_query(monkeypatch, stamps, delay=0.0,
                      raises=llm.EmbedUnavailable("sidecar says no", reason="busy"))

    assert asyncio.run(memory_semantic.semantic_hits(account, PLAIN_QUESTION, "cold")) == []
    assert dropped() == {}
    assert wasted() == 0
    assert llm._EMBED_INFLIGHT == {}


def test_an_unavailable_embedding_raised_while_the_load_still_runs_gives_the_same_result(
    account, monkeypatch
):
    stamps = Stamps()

    async def scenario():
        await warm_key(account, "new-conv")
        stamp_legs(monkeypatch, stamps, load_delay=0.06)
        stamp_embed_query(monkeypatch, stamps, delay=0.0,
                          raises=llm.EmbedUnavailable("sidecar says no", reason="busy"))
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    assert asyncio.run(scenario()) == []
    # The embedding failed BEFORE the load returned, and the result is still
    # HEAD's: an empty hit list, no drop label of its own.
    assert stamps.first("embed")[1] < stamps.first("load")[1]
    assert dropped() == {}
    assert wasted() == 0
    assert llm._EMBED_INFLIGHT == {}


def test_a_failing_candidate_load_counts_and_cancels_the_speculative_embedding(
    account, monkeypatch
):
    """CHANGED 2026-09-27, and the change is the point of it.

    This test used to assert `wasted() == 0` here, on the reasoning that a
    failed load degrades the turn on HEAD too so the guess was not "wrong".
    That reasoning was wrong, and the assertion was pinning a defect: HEAD
    starts NO embedding on this path, the branch has already put one on a
    sidecar shared with every other user of the box, and the counter that is
    this change's declared kill switch could not see it. The documented failure
    mode is Postgres out of connection slots — which makes the load raise on
    EVERY turn, so this is the one reason that can climb fast, and it was the
    one reading zero.

    It now asserts the corrected behaviour: counted, under `load_failed`, and
    still cancelled rather than awaited. It therefore also fails on dev (where
    no counter exists) for a real reason, which the version it replaces did
    not — that one passed on dev vacuously, because with no speculation there
    is nothing to cancel and every assertion held trivially.
    """
    stamps = Stamps()
    started = {"n": 0}

    async def scenario():
        await warm_key(account, "new-conv")
        stamp_embed_query(monkeypatch, stamps, delay=0.5)

        def exploding(*a, **k):
            started["n"] += 1
            raise db.OperationalError("no connection slots left")

        monkeypatch.setattr(memory_semantic, "_load_candidates", exploding)
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    began = time.perf_counter()
    assert asyncio.run(scenario()) == []
    elapsed = time.perf_counter() - began
    assert started["n"] == 1
    assert dropped() == {}
    assert llm._EMBED_INFLIGHT == {}
    # THE REGRESSION THIS TEST NOW GUARDS: one embedding HEAD never sends, on
    # the wire, visible to the kill switch and attributed to the right cause.
    assert wasted_by_reason() == {"load_failed": 1}
    # Cancelled, not awaited: the 0.5 s embedding never held the turn.
    assert elapsed < 0.45, f"the failing load waited {elapsed:.3f}s for the embedding"


def _embedding_that_counts_itself(monkeypatch, counter: dict, delay: float = 0.5):
    """A query-embedding double that records whether its body ever RAN.

    `asyncio.ensure_future` only schedules; the loop has to give the coroutine a
    turn before anything reaches the sidecar. That distinction is the subject of
    the two tests below, so it is counted inside the coroutine, not at the call.
    """

    async def embed_query(text, **kw):
        counter["n"] += 1
        await asyncio.sleep(delay)
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(llm, "embed_query", embed_query)


def test_load_failed_counts_a_request_that_really_was_sent_on_the_documented_failure(
    account, monkeypatch
):
    """THE KILL SWITCH'S WORST REASON MUST COUNT REAL REQUESTS.

    `load_failed` is the reason that can climb fast, because the documented
    failure — PostgreSQL out of connection slots — makes the candidate load raise
    on every turn. That failure raises inside `_load_candidates`, i.e. inside the
    worker thread, which means `db.run_in_thread` has already suspended and the
    speculative embedding's coroutine HAS run: what the counter records is a
    request that was really put on the shared sidecar. Asserted here rather than
    assumed, because the comment beside the counter asserts it in prose.
    """
    embed_started = {"n": 0}

    async def scenario():
        await warm_key(account, "new-conv")
        _embedding_that_counts_itself(monkeypatch, embed_started)
        embed_started["n"] = 0        # only the MEASURED turn is counted

        def exploding(*a, **k):
            raise db.OperationalError("no connection slots left")

        monkeypatch.setattr(memory_semantic, "_load_candidates", exploding)
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    began = time.perf_counter()
    assert asyncio.run(scenario()) == []
    elapsed = time.perf_counter() - began
    assert embed_started["n"] == 1, "the counted request was never actually sent"
    assert wasted_by_reason() == {"load_failed": 1}
    # Cancelled, not awaited: the 0.5 s embedding never held the turn.
    assert elapsed < 0.45, f"the failing load waited {elapsed:.3f}s for the embedding"


def test_a_synchronous_raise_from_run_in_thread_over_counts_and_that_is_left_as_is(
    account, monkeypatch
):
    """THE ONE SHAPE WHERE `load_failed` OVER-COUNTS (QA, 2026-09-27).

    If `db.run_in_thread` raises before its own first suspension, the loop never
    gives the scheduled embedding a turn: nothing reaches the sidecar, and
    `load_failed` is recorded anyway. This is NOT the documented failure — that
    one raises inside the worker thread and is counted exactly (the test above) —
    and the over-count is deliberately kept, because a kill switch that fires
    early errs the safe way. Pinned here so the behaviour and the comments beside
    the counter cannot drift apart: the comments now say the request is
    *normally* on the wire, and this is the exception they name.
    """
    embed_started = {"n": 0}

    async def scenario():
        await warm_key(account, "new-conv")
        _embedding_that_counts_itself(monkeypatch, embed_started)
        embed_started["n"] = 0

        async def raising_before_suspension(fn, *a, **k):
            raise db.OperationalError("no connection slots left")

        monkeypatch.setattr(db, "run_in_thread", raising_before_suspension)
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    assert asyncio.run(scenario()) == []
    assert embed_started["n"] == 0, (
        "the embedding coroutine ran after all — then this shape is not an "
        "over-count and the comments beside the counter should say so"
    )
    assert wasted_by_reason() == {"load_failed": 1}


def test_a_turn_cancelled_while_the_candidate_load_runs_counts_its_embedding(
    account, monkeypatch
):
    """The second half of the same regression, on the path production reaches
    most often: `reads.close()` in main.py cancels any cross-chat read still in
    flight when the turn goes another way, and a newer message replaces a turn
    outright. The embedding is already on the sidecar's wire by then, so
    cancelling the turn does not un-spend it, and it is counted under its own
    reason rather than sharing one with a stale cache entry."""
    stamps = Stamps()
    real_load = memory_semantic._load_candidates

    async def scenario():
        await warm_key(account, "new-conv")
        stamp_embed_query(monkeypatch, stamps, delay=0.5)

        def slow(*a, **k):
            time.sleep(0.15)
            return real_load(*a, **k)

        monkeypatch.setattr(memory_semantic, "_load_candidates", slow)
        turn = asyncio.ensure_future(
            memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")
        )
        await asyncio.sleep(0.03)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(scenario())
    assert wasted_by_reason() == {"cancelled": 1}
    assert llm._EMBED_INFLIGHT == {}


# ---------------------------------------------------------------------------
# (7) the switch itself: off in production, and off means HEAD exactly
# ---------------------------------------------------------------------------
def test_the_speculation_is_off_by_default_and_the_default_keeps_head_s_order(
    account, monkeypatch
):
    """The flag's default is the behaviour production runs, so it is asserted
    here and not left to a comment. With it off, a WARM candidate cache — the
    one state in which the gate would otherwise fire — still embeds strictly
    after the load returns, exactly as dev does, and nothing is counted."""
    assert memory_semantic.CROSS_CHAT_SPECULATIVE_EMBED is False
    from app import config as _config

    assert _config.Settings().cross_chat_speculative_embed is False

    stamps = Stamps()

    async def scenario():
        await warm_key(account, "new-conv")
        # Undo the fixture's override: this test wants the shipped default.
        monkeypatch.setattr(settings, "cross_chat_speculative_embed", False, raising=False)
        stamp_legs(monkeypatch, stamps, load_delay=0.04)
        stamp_embed_query(monkeypatch, stamps, delay=0.04)
        return await memory_semantic.semantic_hits(account, PLAIN_QUESTION, "new-conv")

    asyncio.run(scenario())

    assert stamps.count("load") == 1 and stamps.count("embed") == 1
    assert stamps.overlap_ms("load", "embed") < 0.0, (
        "with the flag off the embedding must start after the load returns, "
        f"but the legs overlapped by {stamps.overlap_ms('load', 'embed'):.3f} ms"
    )
    assert wasted_by_reason() == {}
    assert llm._EMBED_INFLIGHT == {}


# ---------------------------------------------------------------------------
# (6) a stalled sidecar leaves no EMBED_MAX_INFLIGHT slot held
# ---------------------------------------------------------------------------
def test_a_stalled_sidecar_does_not_make_the_next_turn_busy(account, monkeypatch):
    monkeypatch.setattr(settings, "embed_max_inflight", 1)
    monkeypatch.setattr(settings, "embed_wait_s", 0.4)
    monkeypatch.setattr(settings, "embed_timeout_s", 5.0)
    state = {"stall": False}

    async def embed_texts(texts, **kw):
        if state["stall"]:
            await asyncio.sleep(30.0)
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def scenario():
        monkeypatch.setattr(llm, "embed_texts", embed_texts)
        await warm_key(account, "new-conv")       # fills the candidate cache
        llm.embed_cache_clear()                   # the next question is new
        state["stall"] = True
        # The cache says this key has rows, so the embedding starts early; the
        # refetch then comes back empty and the embedding is cancelled.
        monkeypatch.setattr(memory_semantic, "_load_candidates", lambda *a, **k: [])
        assert await memory_semantic.semantic_hits(account, "what about the ceo", "new-conv") == []
        state["stall"] = False
        began = time.perf_counter()
        vector = await llm.embed_query("a completely different question about bread")
        return vector, time.perf_counter() - began

    vector, elapsed = asyncio.run(scenario())
    assert vector == [1.0, 0.0, 0.0]
    assert elapsed < 0.4, f"the next turn waited {elapsed:.3f}s — the slot was still held"
    busy = [
        value for key, value in (metrics._counters.get("embed_requests_total") or {}).items()
        if dict(key).get("outcome") == "busy"
    ]
    assert busy == [], f"embed_requests_total{{outcome=busy}} moved: {busy}"
    assert wasted() == 1
    assert llm._EMBED_INFLIGHT == {}
