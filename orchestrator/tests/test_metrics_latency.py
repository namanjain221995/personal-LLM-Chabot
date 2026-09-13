"""The 1-2 s programme's measurement (performance plan item 1, 2026-09-13).

Without these the owner's goal — answers start within 1-2 seconds — cannot be
read off Prometheus: chat_ttft_seconds jumped from le=1.0 to le=2.5, every
knowledge retrieval stage folded into stage="other" after 2c51487, and the
pre-pass steps had no histograms at all. Pure registry tests: no database, no
engine.
"""
from __future__ import annotations

import pytest

from app import metrics

LATENCY_HISTOGRAMS = (
    "chat_first_visible_seconds",
    "knowledge_prepare_seconds",
    "context_assembly_seconds",
    "orchestrate_decide_seconds",
    "relay_overhead_seconds",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    metrics.reset()
    yield
    metrics.reset()


def _bucket_lines(name: str) -> list:
    return [line for line in metrics.render().splitlines() if line.startswith(name + "_bucket")]


def test_chat_ttft_has_a_bucket_at_one_and_a_half_and_at_two_seconds():
    assert metrics._buckets_for("chat_ttft_seconds") == (
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 5.0, 10.0, 30.0,
    )


def test_a_ttft_of_1_4_seconds_is_counted_under_1_5_and_not_under_1_0():
    metrics.observe("chat_ttft_seconds", 1.4, route="chat", effort="fast")
    lines = _bucket_lines("chat_ttft_seconds")
    assert 'chat_ttft_seconds_bucket{effort="fast",route="chat",le="1.0"} 0' in lines
    assert 'chat_ttft_seconds_bucket{effort="fast",route="chat",le="1.5"} 1' in lines
    assert 'chat_ttft_seconds_bucket{effort="fast",route="chat",le="2.0"} 1' in lines


def test_the_ttft_buckets_keep_every_default_edge_so_existing_series_continue():
    assert set(metrics._BUCKETS) <= set(metrics._buckets_for("chat_ttft_seconds"))
    assert set(metrics._buckets_for("chat_ttft_seconds")) - set(metrics._BUCKETS) == {1.5, 2.0}


def test_the_new_buckets_are_per_metric_and_leave_the_default_set_alone():
    assert metrics._BUCKETS == (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
    for name in ("techsara_web_memory_seconds", "freshness_router_seconds", "knowledge_stage_seconds",
                 "chat_total_seconds", "context_assembly_seconds", "relay_overhead_seconds"):
        assert metrics._buckets_for(name) == metrics._BUCKETS, name
    assert metrics._buckets_for("llm_admission_wait_seconds") == metrics._WAIT_BUCKETS


def test_first_visible_and_knowledge_prepare_are_read_on_the_ttft_edges():
    assert metrics._buckets_for("chat_first_visible_seconds") == metrics._buckets_for("chat_ttft_seconds")
    assert metrics._buckets_for("knowledge_prepare_seconds") == metrics._buckets_for("chat_ttft_seconds")


@pytest.mark.parametrize("stage", ["lexical", "dense_scan", "embed", "meta", "rerank", "servable"])
def test_each_knowledge_retrieval_stage_is_its_own_series_again(stage):
    metrics.observe("knowledge_stage_seconds", 0.1, stage=stage)
    assert (("stage", stage),) in metrics._hists["knowledge_stage_seconds"]
    assert (("stage", "other"),) not in metrics._hists["knowledge_stage_seconds"]


def test_an_unknown_knowledge_stage_still_folds_to_other():
    metrics.observe("knowledge_stage_seconds", 0.1, stage="https://example.com/page")
    assert list(metrics._hists["knowledge_stage_seconds"]) == [(("stage", "other"),)]


def test_knowledge_stages_are_not_valid_video_or_artifact_stages():
    metrics.observe("video_stage_seconds", 1.0, stage="lexical")
    assert list(metrics._hists["video_stage_seconds"]) == [(("stage", "other"),)]
    assert "lexical" not in metrics._ALLOWED["stage"]


def test_the_escalation_counter_keeps_its_two_stages():
    metrics.inc("knowledge_escalation_total", effort="think", stage="local_first")
    metrics.inc("knowledge_escalation_total", effort="think", stage="search")
    keys = set(metrics._counters["knowledge_escalation_total"])
    assert keys == {(("effort", "think"), ("stage", "local_first")), (("effort", "think"), ("stage", "search"))}


def test_every_new_latency_histogram_is_registered_with_a_closed_label_set():
    for name in LATENCY_HISTOGRAMS:
        assert name in metrics._LABELS_BY_METRIC, name
        for label, values in metrics._LABELS_BY_METRIC[name].items():
            assert values and len(values) <= 32, (name, label)


def test_the_helpers_emit_each_new_histogram_under_its_exact_name():
    metrics.chat_first_visible(0.4, route="chat", effort="think", kind="reasoning")
    metrics.knowledge_prepare(0.3, effort="fast", decision="static_model", outcome="deadline")
    metrics.context_assembly(0.2, effort="fast", mode="assistant")
    metrics.orchestrate_decide(0.25, effort="max", plan=metrics.plan_label(True, False))
    metrics.relay_overhead(0.01, route="chat", effort="fast")
    text = metrics.render()
    for name in LATENCY_HISTOGRAMS:
        assert f"# TYPE {name} histogram" in text, name
    assert 'chat_first_visible_seconds_count{effort="think",kind="reasoning",route="chat"} 1' in text
    assert 'knowledge_prepare_seconds_count{decision="static_model",effort="fast",outcome="deadline"} 1' in text
    assert 'context_assembly_seconds_count{effort="fast",mode="assistant"} 1' in text
    assert 'orchestrate_decide_seconds_count{effort="max",outcome="ok",plan="agent"} 1' in text
    assert 'relay_overhead_seconds_count{effort="fast",route="chat"} 1' in text


@pytest.mark.parametrize("name", LATENCY_HISTOGRAMS)
def test_a_user_or_key_id_passed_to_a_latency_histogram_never_becomes_a_label(name):
    metrics.observe(
        name, 0.5, user_id="u-123", api_key_id="kkkkkkkk", conversation_id="c-9", effort="fast",
    )
    (key,) = metrics._hists[name]
    names = {label for label, _ in key}
    assert names <= set(metrics._LABELS_BY_METRIC[name])
    assert not names & {"user_id", "api_key_id", "conversation_id"}
    assert "u-123" not in metrics.render() and "kkkkkkkk" not in metrics.render()


@pytest.mark.parametrize("name", LATENCY_HISTOGRAMS)
def test_a_free_text_value_on_a_latency_histogram_folds_to_other(name):
    labels = {label: "What is the price of gold today?" for label in metrics._LABELS_BY_METRIC[name]}
    metrics.observe(name, 0.5, **labels)
    (key,) = metrics._hists[name]
    assert all(value == "other" for _, value in key)


def test_a_thousand_distinct_values_mint_at_most_one_extra_series():
    for i in range(1000):
        metrics.relay_overhead(0.01, route=f"route-{i}", effort=f"effort-{i}")
    assert list(metrics._hists["relay_overhead_seconds"]) == [(("effort", "other"), ("route", "other"))]


def test_the_route_vocabulary_holds_every_route_the_router_engine_can_pick():
    from app.engines import router

    picked = set(router._ROUTE_RE.pattern.split("(")[1].split(")")[0].split("|"))
    assert picked <= metrics.CHAT_ROUTES


def test_the_effort_vocabulary_is_the_chat_requests_own():
    import typing

    from app.main import ChatRequest

    literal = ChatRequest.model_fields["effort"].annotation
    assert set(typing.get_args(literal)) == set(metrics.CHAT_EFFORTS)


def test_the_context_mode_vocabulary_is_the_chat_requests_own():
    import typing

    from app.main import ChatRequest

    literal = ChatRequest.model_fields["mode"].annotation
    assert set(typing.get_args(literal)) == metrics._LABELS_BY_METRIC["context_assembly_seconds"]["mode"]


def test_the_decision_vocabulary_is_what_living_knowledge_decides():
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[1].joinpath("app", "living_knowledge.py").read_text()
    decided = {
        value
        for line in source.splitlines()
        if "_decided(out," in line
        for value in re.findall(r'"([a-z_]+)"', line)
    }
    assert "escalate_search" in decided and "static_model" in decided
    assert decided <= metrics.KNOWLEDGE_DECISIONS


def test_plan_label_flattens_every_plan_into_the_closed_set():
    seen = {metrics.plan_label(a, s) for a in (False, True) for s in (False, True)}
    assert seen == metrics.DECIDE_PLANS


def test_the_existing_chat_ttft_labels_are_not_narrowed():
    metrics.observe("chat_ttft_seconds", 0.5, route="brand_new_engine", effort="fast")
    assert (("effort", "fast"), ("route", "brand_new_engine")) in metrics._hists["chat_ttft_seconds"]


# ── the knowledge team's counters (2026-09-13 revision) ─────────────────────


def test_the_freshness_rule_vocabulary_holds_the_router_skip_and_not_the_withdrawn_rule():
    from app import freshness

    assert freshness.TIMELESS_TASK_REASON in metrics._ALLOWED["rule"]
    assert "no_time_signal" not in metrics._ALLOWED["rule"]
    metrics.freshness_classified("static", freshness.TIMELESS_TASK_REASON)
    assert (("level", "static"), ("rule", "timeless_task")) in metrics._counters["techsara_freshness_classified_total"]


@pytest.mark.parametrize("result", ["hit", "miss", "fail"])
def test_each_topical_precheck_answer_is_its_own_series(result):
    metrics.topical_precheck(result)
    assert metrics._counters["knowledge_topical_precheck_total"] == {(("result", result),): 1.0}


def test_a_topical_precheck_result_outside_its_three_values_folds_to_other():
    metrics.inc("knowledge_topical_precheck_total", result="ok", user_id="u-1")
    assert metrics._counters["knowledge_topical_precheck_total"] == {(("result", "other"),): 1.0}


@pytest.mark.parametrize("reason", ["embed_busy", "embed_timeout", "embed_error"])
def test_the_recall_drop_counter_takes_relays_bare_inc_call_under_its_exact_name(reason):
    """RELAY's call site is metrics.inc("recall_block_dropped_total", reason=...)."""
    metrics.inc("recall_block_dropped_total", reason=reason)
    assert metrics._counters["recall_block_dropped_total"] == {(("reason", reason),): 1.0}
    text = metrics.render()
    assert f'recall_block_dropped_total{{reason="{reason}"}} 1' in text
    assert "# TYPE recall_block_dropped_total counter" in text


def test_the_recall_drop_counter_keeps_its_help_text_when_the_bare_inc_reaches_it_first():
    metrics.inc("recall_block_dropped_total", reason="embed_busy")
    assert "# HELP recall_block_dropped_total In-conversation recall blocks" in metrics.render()


def test_a_recall_drop_with_a_stray_reason_or_label_mints_no_new_series():
    for i in range(50):
        metrics.inc("recall_block_dropped_total", reason=f"boom-{i}", conversation_id=f"c-{i}")
    metrics.recall_block_dropped("embed_timeout")
    assert set(metrics._counters["recall_block_dropped_total"]) == {
        (("reason", "other"),), (("reason", "embed_timeout"),),
    }


def test_the_contract_counters_are_declared_at_import():
    assert metrics._TYPE.get("recall_block_dropped_total") == "counter"
    assert metrics._TYPE.get("knowledge_topical_precheck_total") == "counter"
    assert metrics.RECALL_DROP_REASONS == {"embed_busy", "embed_timeout", "embed_error"}
    assert metrics.TOPICAL_PRECHECK_RESULTS == {"hit", "miss", "fail"}


def test_an_unregistered_counter_name_never_raises():
    metrics.inc("a_counter_nobody_registered_total", reason="embed_busy")
    assert metrics._counters["a_counter_nobody_registered_total"]
