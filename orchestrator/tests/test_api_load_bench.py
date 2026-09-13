"""tools/api_load_bench.py — the admission lanes under simulated load (2026-09-13).

Stub engine only, virtual time, no network: each scenario drives the REAL
`admission.run` for an hour or more of simulated traffic in about a second
of CPU. What is pinned: the mixed scenario never preempts the stub engine,
never commits KV past the budget, keeps every wait inside its bound, and
chat waits no longer than /v1 on average; two 1M answers never hold KV
together; a /v1 flood never refuses a chat turn nor takes the reserved seat;
a burst of long answers leaves chat TTFT p95 unchanged at 0.15 chat/s, while
the same burst in a shared NORMAL lane (no LONG_OUTPUT lane) multiplies it;
the virtual clock cannot stand still; the HTTP stub refuses anything but
loopback and never takes a production port. And one scenario per finding of
the adversarial review of 2026-09-13: a /v1 flood refuses no chat turn the
engine serves alone; a /v1 burst keeps chat's wait bounded; documents queued
before a 1M request get their KV first and the request is not starved; a /v1
document loop closes chat's lane only within its budget; small /v1 answers
are not held behind documents that cannot fit; chat long answers pass /v1
heads that cannot; /v1 NORMAL requests with 131K prompts beside a 1M answer
never preempt the stub, and would without the managed limit.
"""
from __future__ import annotations

import pytest

from tools import api_load_bench as bench


def test_the_mixed_scenario_ends_with_zero_stub_preemptions_and_chat_waits_no_longer_than_v1():
    report = bench.run_scenario("mixed", bench.Config())
    for label, run in report["runs"].items():
        assert run["stub"]["preemptions"] == 0, label
        assert run["kv"]["over_budget_with_more_than_one_charge"] == 0, label
        assert run["kv"]["committed_max_tokens"] <= run["kv"]["budget_tokens"], label
        assert run["waits_within_bound"] is True, label
        normal = run["normal_wait_s_by_origin"]
        assert normal["chat"]["n"] > 100 and normal["v1"]["n"] > 50, label
        # On average: the tails of both classes are set by the same LONG
        # closure (a large document's prefill), which neither can jump.
        assert normal["chat"]["mean"] <= normal["v1"]["mean"], (label, normal)
        assert normal["chat"]["p50"] <= normal["v1"]["p50"], (label, normal)
    with_long = report["runs"]["with"]
    assert with_long["lane_active_max"]["long_output"] == 2
    assert with_long["lane_active_max"]["normal_v1"] <= 9
    assert with_long["requests"]["v1_long"] > 0


def test_two_one_million_token_answers_never_hold_kv_together_and_the_second_is_refused_after_its_bound():
    run = bench.run_scenario("two_1m", bench.Config())["runs"]["with"]
    assert run["kv"]["committed_max_tokens"] == 1_008_176
    assert run["lane_active_max"]["long_output"] == 1
    assert run["rejected"] == {"long_output/timeout": 1}
    assert run["longest_v1_wait_s"] == pytest.approx(600.0, abs=1.5)
    assert run["stub"]["preemptions"] == 0


def test_a_v1_flood_never_refuses_a_chat_turn_and_never_holds_the_reserved_seat():
    run = bench.run_scenario("v1_flood", bench.Config(duration=600.0))["runs"]["with"]
    assert run["admitted"]["chat"] == run["requests"]["chat"]
    assert run["rejected"].get("normal/capacity", 0) > 0, "the flood itself is refused at its own door"
    assert run["lane_active_max"]["normal_v1"] == 8, "the two reserved seats (default 2) stay free of /v1"
    assert run["waits_within_bound"] is True


def test_a_long_output_burst_leaves_chat_ttft_p95_unchanged_and_a_shared_lane_multiplies_it():
    # 0.15 chat/s: the first build was +27 % here (+164 % pooled over 20 seeds);
    # the seat share and headroom rule of the review keep it at 1.0 (this seed).
    build = bench.run_scenario("long_burst", bench.Config(chat_rate=0.15))
    assert build["chat_ttft_p95_ratio_with_over_baseline"] <= 1.10
    assert build["runs"]["with"]["lane_active_max"]["long_output"] == 2
    # The counterfactual: every /v1 answer in NORMAL, as before the lane.
    shared = bench.run_scenario("long_burst", bench.Config(chat_rate=0.15, v1_long_output_threshold=10**9))
    assert shared["runs"]["with"]["lane_active_max"]["long_output"] == 0
    assert shared["chat_ttft_p95_ratio_with_over_baseline"] >= 2.0  # 4.58 at this seed


def test_a_v1_flood_refuses_no_chat_turn_the_engine_serves_alone():
    # First build, 1,800 s: 541 of 911 chat turns admitted, chat wait p50 597.7 s.
    report = bench.run_scenario("chat_vs_v1_flood", bench.Config(duration=600.0))
    run, alone = report["runs"]["with"], report["runs"]["baseline"]
    assert run["admitted"]["chat"] == run["requests"]["chat"] == alone["admitted"]["chat"]
    assert run["rejected_by_population"]["v1_short"] > 0, "the flood is refused, not chat"
    assert run["normal_wait_s_by_origin"]["chat"]["mean"] <= 15.0  # 9.3 at this seed
    assert run["lane_active_max"]["normal_v1"] <= 8, "two seats stay free while /v1 is above its share"


def test_a_v1_burst_of_long_short_answers_keeps_chat_waits_bounded():
    # First build: chat wait p95 76.3 s (grant-count weight, one reserved seat as a /v1 cap).
    run = bench.run_scenario("v1_burst_then_chat", bench.Config(duration=600.0))["runs"]["with"]
    assert run["admitted"]["chat"] == run["requests"]["chat"]
    assert run["wait_s_by_origin"]["chat"]["p95"] <= 40.0  # 26.7 at this seed
    assert run["wait_s_by_origin"]["chat"]["p50"] == 0.0


def test_documents_queued_before_a_1m_request_get_their_kv_first_and_the_request_is_not_starved():
    run = bench.run_scenario("chat_docs_back_to_back_1m", bench.Config(keep_timeline=True))["runs"]["with"]
    before = [row for row in run["timeline"] if row[0] == "chat_long" and row[1] < 100.0]
    assert len(before) == 3 and all(row[2] is not None for row in before), before
    v1 = [row for row in run["timeline"] if row[0] == "v1_long"]
    assert v1[0][2] is not None
    # Promoted after 300 s (chat documents were what kept it out), in at the next release.
    assert v1[0][2] - v1[0][1] < 300.0 + 60.0, v1
    assert run["stub"]["preemptions"] == 0


def test_a_v1_document_loop_closes_chat_normal_only_within_its_budget():
    budgeted = bench.run_scenario("v1_doc_loop", bench.Config(duration=7200.0))
    unbudgeted = bench.run_scenario("v1_doc_loop", bench.Config(duration=7200.0, v1_closure_window_s=0.0))
    run, free_for_all = budgeted["runs"]["with"], unbudgeted["runs"]["with"]
    assert run["admitted"]["v1_doc"] == 1 and free_for_all["admitted"]["v1_doc"] == 8
    assert run["rejected"] == {"long/timeout": 7}
    chat, before = run["normal_wait_s_by_origin"]["chat"], free_for_all["normal_wait_s_by_origin"]["chat"]
    assert chat["p50"] == 0.0 and before["p50"] > 300.0
    assert chat["mean"] < before["mean"] / 4
    assert run["admitted"]["chat"] == run["requests"]["chat"]


def test_small_v1_long_answers_are_not_held_behind_chat_documents_that_cannot_fit():
    # First build: 7 of 9 admitted, a 600 s wait behind each document.
    run = bench.run_scenario("doomed_chat_doc_blocks_v1", bench.Config())["runs"]["with"]
    assert run["admitted"]["v1_small_long"] == run["requests"]["v1_small_long"] == 9
    assert run["wait_s_by_lane"]["long_output"]["max"] == 0.0


def test_chat_long_answers_pass_v1_heads_that_cannot_fit_and_the_v1_result_is_unchanged():
    run = bench.run_scenario("v1_drain_blocks_chat_long_answers", bench.Config())["runs"]["with"]
    assert run["admitted"]["chat_long_answer"] == run["requests"]["chat_long_answer"] == 30
    assert run["admitted"]["v1_long"] == 1, "the 1M requests fit only once the 400K job is gone, as before"
    assert run["kv"]["committed_max_tokens"] <= run["kv"]["budget_tokens"]


def test_v1_big_prompts_beside_a_1m_answer_never_preempt_and_would_without_the_managed_limit():
    limited = bench.run_scenario("v1_big_prompts_beside_1m", bench.Config())["runs"]["with"]
    unlimited = bench.run_scenario("v1_big_prompts_beside_1m", bench.Config(v1_normal_kv=False))["runs"]["with"]
    assert limited["stub"]["preemptions"] == 0 and limited["stub"]["kv_peak_blocks"] < 799
    assert unlimited["stub"]["preemptions"] > 0  # 15 at this seed, the pool full at 799 blocks
    assert limited["admitted"]["chat"] == limited["requests"]["chat"]


def test_the_virtual_clock_moves_on_every_read():
    clock = bench.VirtualClock()
    first = clock.time()
    assert clock.time() > first, "a clock that can stand still lets a nearly expired waiter spin forever"


@pytest.mark.parametrize("target", ["0.0.0.0:29100", "192.168.1.20:29100", "example.com:29100",
                                    "127.0.0.1:8000", "127.0.0.1:8080", "127.0.0.1:9090", "127.0.0.1:9838",
                                    "localhost:30004", "[::1]:8002", "127.0.0.1", "127.0.0.1:0"])
def test_serve_stub_refuses_a_non_loopback_host_and_production_ports(target, capsys):
    with pytest.raises(SystemExit) as refused:
        bench.main(["--serve-stub", target])
    assert refused.value.code == 2
    assert "--serve-stub" in capsys.readouterr().err


def test_serve_stub_accepts_a_private_loopback_port():
    assert bench.parse_serve_target("127.0.0.1:29100") == ("127.0.0.1", 29100)
    assert bench.parse_serve_target("[::1]:29101") == ("::1", 29101)
