"""The in-process session memory keeps each exchange's mode (design_v2 G2b/C5).

A client that sends no turns and no conversation id reads the account-wide key,
which can hold a Salesforce-mode exchange from another request. A toggle-off
turn asks for the history without it; a Salesforce-mode follow-up still sees
everything.
"""
from __future__ import annotations

from app.memory import SessionMemory


def test_salesforce_pairs_are_dropped_on_request_and_the_order_is_kept():
    mem = SessionMemory(max_turns=10)
    mem.add_exchange("u1-default", "hello", "hi there", mode="assistant")
    mem.add_exchange("u1-default", "how many placements?", "Ravi closed 14.", mode="salesforce")
    mem.add_exchange("u1-default", "tell me a joke", "a joke", mode="assistant")
    assert mem.history("u1-default", exclude_salesforce=True) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "tell me a joke"},
        {"role": "assistant", "content": "a joke"},
    ]
    everything = mem.history("u1-default")
    assert [m["content"] for m in everything] == [
        "hello", "hi there", "how many placements?", "Ravi closed 14.", "tell me a joke", "a joke",
    ]
    assert all(set(m) == {"role", "content"} for m in everything), "turns reach the model as {role, content}"


def test_an_exchange_stored_without_a_mode_is_kept():
    mem = SessionMemory(max_turns=10)
    mem.add_exchange("s", "q", "a")
    assert mem.history("s", exclude_salesforce=True) == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


def test_trimming_still_counts_exchanges():
    mem = SessionMemory(max_turns=2)
    for i in range(4):
        mem.add_exchange("s", f"q{i}", f"a{i}", mode="salesforce" if i % 2 else "assistant")
    assert [m["content"] for m in mem.history("s")] == ["q2", "a2", "q3", "a3"]
    assert [m["content"] for m in mem.history("s", exclude_salesforce=True)] == ["q2", "a2"]
