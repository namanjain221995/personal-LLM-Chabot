"""app/kv_budget.py — the engine's KV cache as a budget (2026-09-13).

Offline: the one network call (`_fetch_text`) is replaced in every test that
reaches it. What is pinned: the live `vllm:cache_config_info` line parses to
the 2026-09-13 layout; a 1M-token request is charged 481 whole blocks (3 fixed
GDN state blocks + 478 attention blocks); the default budget admits one full
window and never two; the pool read is cached, single-flight, and falls back
to the last live value, then the settings; the settings helpers parse the
environment exactly as config.py does; the reserve is clamped.
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app import kv_budget
from app.config import settings

#: The 2026-09-13 series from Prometheus (`vllm:cache_config_info{job="vllm-main"}`,
#: read-only), minus the scrape-target labels Prometheus adds (instance, job,
#: role, service), written as the engine's exposition line.
LIVE_LINE = (
    'vllm:cache_config_info{_block_size_resolved="True",block_size="2096",cache_dtype="fp8",'
    'enable_mamba_fine_grained_prefix_cache="False",enable_prefix_caching="False",engine="0",'
    'engine_scope="cluster",gpu_memory_utilization="0.3",is_attention_free="False",'
    'kv_cache_dtype_skip_layers="[]",kv_cache_layout="None",kv_cache_max_concurrency="1.6632016632016633",'
    'kv_cache_memory_bytes="8589934592",kv_cache_size_tokens="1663201",kv_offloading_backend="native",'
    'kv_offloading_size="None",kv_sharing_fast_prefill="False",mamba_block_size="1000000",'
    'mamba_cache_dtype="auto",mamba_cache_mode="none",mamba_page_size_padded="None",'
    'mamba_ssm_cache_dtype="float32",num_cpu_blocks="None",num_gpu_blocks="800",'
    'num_gpu_blocks_override="None",prefix_cache_retention_interval="0",prefix_caching_hash_algo="sha256",'
    'prefix_match_unit="None",replayssm_buffer_len="16",skip_page_size_padded="None",sliding_window="None",'
    'use_kda_recoverssm="False",use_replayssm="False",user_specified_block_size="False",'
    'user_specified_mamba_block_size="False"} 1.0'
)

METRICS_TEXT = "\n".join([
    "# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig",
    "# TYPE vllm:cache_config_info gauge",
    LIVE_LINE,
    'vllm:num_requests_running{engine="0",model_name="m"} 3.0',
])

_NEW_SETTINGS = (
    "admission_kv_unmanaged_headroom_fraction", "admission_kv_reserve_fraction", "admission_kv_pool_tokens", "admission_kv_block_size",
    "admission_kv_fixed_blocks_per_seq", "admission_kv_pool_refresh_s", "admission_kv_metrics_url",
)
_NEW_ENV = (
    "ADMISSION_KV_UNMANAGED_HEADROOM_FRACTION", "ADMISSION_KV_RESERVE_FRACTION", "ADMISSION_KV_POOL_TOKENS", "ADMISSION_KV_BLOCK_SIZE",
    "ADMISSION_KV_FIXED_BLOCKS_PER_SEQ", "ADMISSION_KV_POOL_REFRESH_S", "ADMISSION_KV_METRICS_URL",
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    # The defaults under test are the code's own, whatever config.py or the
    # environment of the machine running the suite says.
    for name in _NEW_SETTINGS:
        monkeypatch.delattr(settings, name, raising=False)
    for name in _NEW_ENV:
        monkeypatch.delenv(name, raising=False)
    kv_budget.reset()

    async def no_network(url):
        raise AssertionError(f"unexpected network read of {url}")

    monkeypatch.setattr(kv_budget, "_fetch_text", no_network)
    yield
    kv_budget.reset()


def test_the_live_cache_config_line_parses_to_1663201_tokens_in_2096_token_blocks():
    pool = kv_budget.parse_cache_config(METRICS_TEXT)
    assert pool is not None
    assert (pool.tokens, pool.block_size, pool.source) == (1_663_201, 2096, "live")
    # A line without both labels is not a pool; nothing at all is None.
    assert kv_budget.parse_cache_config('vllm:cache_config_info{block_size="2096"} 1.0') is None
    assert kv_budget.parse_cache_config("") is None
    # The first line carrying both wins (one per engine index on a multi-engine server).
    second = LIVE_LINE.replace('kv_cache_size_tokens="1663201"', 'kv_cache_size_tokens="5"')
    assert kv_budget.parse_cache_config(LIVE_LINE + "\n" + second).tokens == 1_663_201


def test_a_one_million_token_request_is_charged_481_blocks():
    charge = kv_budget.charge_tokens(1_000, 1_000_000, block_size=2096, window=1_000_000)
    # min(1,001,000, 1,000,000) = 1,000,000 → ceil(1e6 / 2096) = 478, + 3 fixed.
    assert charge == 481 * 2096 == 1_008_176
    # The needle cross-check: 950,000 tokens peaked at 457 blocks in production.
    assert kv_budget.charge_tokens(950_000, 0, block_size=2096, window=1_000_000) == 457 * 2096
    # The smallest sequence holds 4 blocks (measured minimum per sequence).
    assert kv_budget.charge_tokens(10, 2, block_size=2096, window=1_000_000) == 4 * 2096
    # No window: the whole projected length.
    assert kv_budget.charge_tokens(0, 2_000_000, block_size=2096, window=None) == (3 + math.ceil(2_000_000 / 2096)) * 2096


def test_the_default_budget_admits_one_full_window_and_never_two():
    pool = kv_budget.Pool(tokens=1_663_201, block_size=2096, source="live", read_at=0.0)
    budget = kv_budget.budget_tokens(pool)
    assert budget == 1_081_080  # floor(0.65 × 1,663,201)
    full = kv_budget.charge_tokens(10, 1_000_000, block_size=2096, window=1_000_000)
    ledger = kv_budget.Ledger()
    assert ledger.fits(full, budget)
    ledger.commit("a", full, "long_output", "v1")
    assert not ledger.fits(full, budget), "two full windows (2,016,352) never fit"
    # Beside it: 72,904 tokens, i.e. 34 blocks with the 3 fixed ones.
    assert budget - full == 72_904
    assert ledger.fits(kv_budget.charge_tokens(4, 64_000, block_size=2096, window=1_000_000), budget)
    assert not ledger.fits(kv_budget.charge_tokens(4, 66_000, block_size=2096, window=1_000_000), budget)
    # Release is idempotent and exact.
    assert ledger.release("a") is True and ledger.release("a") is False
    assert ledger.committed == 0 and len(ledger) == 0


def test_a_v1_normal_charge_counts_against_the_managed_limit_and_never_the_long_budget(monkeypatch):
    """Adversarial review 2026-09-13: the reserve alone did not bound NORMAL —
    nine 131K-prompt /v1 answers beside a 1M job were 1,111 of 799 blocks."""
    pool = kv_budget.Pool(tokens=1_663_201, block_size=2096, source="live", read_at=0.0)
    budget, limit = kv_budget.budget_tokens(pool), kv_budget.managed_limit_tokens(pool)
    assert (budget, limit) == (1_081_080, 1_413_720)  # floor(0.65 x pool), floor(0.85 x pool)
    full = kv_budget.charge_tokens(10, 1_000_000, block_size=2096, window=1_000_000)
    big_prompt = kv_budget.charge_tokens(131_000, 8_192, block_size=2096, window=1_000_000)
    assert big_prompt == 70 * 2096
    ledger = kv_budget.Ledger()
    ledger.commit("job", full, "long_output", "v1", at=10.0)
    for i in range(2):
        assert ledger.fits_managed(big_prompt, limit)
        ledger.commit(f"n{i}", big_prompt, "normal", "v1", kv_budget.NORMAL_V1, at=20.0 + i)
    assert (ledger.committed, ledger.normal_committed, ledger.managed) == (full, 2 * big_prompt, full + 2 * big_prompt)
    assert not ledger.fits_managed(big_prompt, limit), "a third would pass 1,413,720"
    assert ledger.fits(72_904, budget), "the long budget does not see /v1 NORMAL"
    # What was admitted since a waiter arrived, by kind; and by origin.
    assert ledger.since(15.0) == 2 * big_prompt and ledger.since(0.0, kv_budget.LONG_WORK) == full
    assert ledger.by_origin("v1", kv_budget.NORMAL_V1) == 2 * big_prompt and ledger.by_origin("chat") == 0
    assert ledger.release("n0") and ledger.normal_committed == big_prompt
    monkeypatch.setattr(settings, "admission_kv_unmanaged_headroom_fraction", 2.0, raising=False)
    assert kv_budget.managed_limit_tokens(pool) == int(1_663_201 * 0.05), "clamped to 0.95 like the reserve"


def test_a_request_heavier_than_the_whole_budget_fits_an_empty_ledger_only():
    ledger = kv_budget.Ledger()
    assert ledger.fits(10**9, 1_000)
    ledger.commit("big", 10**9, "long", "chat")
    assert not ledger.fits(1, 1_000)


def test_a_reserve_fraction_outside_zero_to_0_95_is_clamped(monkeypatch):
    pool = kv_budget.Pool(tokens=1_000_000, block_size=2096, source="setting", read_at=0.0)
    monkeypatch.setattr(settings, "admission_kv_reserve_fraction", -0.5, raising=False)
    assert kv_budget.budget_tokens(pool) == 1_000_000, "never more than the pool"
    monkeypatch.setattr(settings, "admission_kv_reserve_fraction", 1.5, raising=False)
    assert kv_budget.budget_tokens(pool) == 50_000, "never less than 5 % of it"
    assert kv_budget.reserve_exhausted({"kv_cache_usage": 0.05})


def test_the_reserve_is_used_up_only_when_the_live_usage_reaches_it():
    assert kv_budget.reserve_exhausted({"kv_cache_usage": 0.65})
    assert not kv_budget.reserve_exhausted({"kv_cache_usage": 0.64})
    assert not kv_budget.reserve_exhausted({"requests_running": 3.0}), "a sample without usage is not evidence"
    assert not kv_budget.reserve_exhausted(None)


def test_an_unreadable_metrics_endpoint_falls_back_to_the_last_live_pool_then_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "admission_kv_pool_refresh_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "admission_kv_pool_tokens", 111_111, raising=False)
    answers = []

    async def fetch(url):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(kv_budget, "_fetch_text", fetch)

    async def run():
        url = "http://vllm-main.test:8000/v1"
        # Never read yet and the read fails: the settings.
        answers.append(ConnectionError("down"))
        first = await kv_budget.pool(url)
        assert (first.tokens, first.source) == (111_111, "setting")
        # A good read: live.
        answers.append(METRICS_TEXT)
        second = await kv_budget.pool(url)
        assert (second.tokens, second.source) == (1_663_201, "live")
        # The endpoint breaks (an error, then a page without the line): the last live value.
        answers.append(ConnectionError("down again"))
        third = await kv_budget.pool(url)
        assert (third.tokens, third.source) == (1_663_201, "live")
        answers.append("# nothing here\n")
        assert (await kv_budget.pool(url)).tokens == 1_663_201
        assert kv_budget.cached().tokens == 1_663_201

    asyncio.run(run())


def test_the_pool_is_read_once_per_refresh_window_by_concurrent_callers(monkeypatch):
    reads = []

    async def fetch(url):
        reads.append(url)
        await asyncio.sleep(0.05)
        return METRICS_TEXT

    monkeypatch.setattr(kv_budget, "_fetch_text", fetch)

    async def run():
        pools = await asyncio.gather(*[kv_budget.pool("http://vllm-main.test:8000/v1") for _ in range(20)])
        assert {p.tokens for p in pools} == {1_663_201}
        assert reads == ["http://vllm-main.test:8000/metrics"], "single-flight"
        # Inside the refresh window: cached, no read.
        await kv_budget.pool("http://vllm-main.test:8000/v1")
        assert len(reads) == 1

        # A caller cancelled while the read is in flight does not cancel it for the others.
        kv_budget.reset()
        waiter = asyncio.ensure_future(kv_budget.pool("http://vllm-main.test:8000/v1"))
        other = asyncio.ensure_future(kv_budget.pool("http://vllm-main.test:8000/v1"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        assert (await other).source == "live"
        assert len(reads) == 2

    asyncio.run(run())


def test_metrics_url_strips_the_v1_suffix_and_honours_the_override(monkeypatch):
    assert kv_budget.metrics_url("http://vllm-main.test:8000/v1") == "http://vllm-main.test:8000/metrics"
    assert kv_budget.metrics_url("http://vllm-main.test:8000/v1/") == "http://vllm-main.test:8000/metrics"
    assert kv_budget.metrics_url("http://vllm-main.test:8000") == "http://vllm-main.test:8000/metrics"
    monkeypatch.setenv("ADMISSION_KV_METRICS_URL", " http://prom.test/engine-metrics ")
    assert kv_budget.metrics_url("http://vllm-main.test:8000/v1") == "http://prom.test/engine-metrics"


def test_metrics_url_off_uses_the_setting_without_any_read(monkeypatch):
    monkeypatch.setenv("ADMISSION_KV_METRICS_URL", "off")

    async def run():
        found = await kv_budget.pool("http://vllm-main.test:8000/v1")
        return found

    found = asyncio.run(run())  # the autouse fixture's fetcher raises on any read
    assert (found.tokens, found.block_size, found.source) == (1_663_201, 2096, "setting")


def test_settings_fall_back_to_the_environment_with_config_parsing(monkeypatch):
    # Not on settings (config.py has not grown the attribute): the environment.
    monkeypatch.setenv("ADMISSION_KV_POOL_TOKENS", "123")
    assert kv_budget.setting_pool_tokens() == 123
    # Blank is the default, exactly like config._int.
    monkeypatch.setenv("ADMISSION_KV_POOL_TOKENS", "   ")
    assert kv_budget.setting_pool_tokens() == 1_663_201
    # Anything else goes through int(): a malformed value raises, it is not guessed at.
    monkeypatch.setenv("ADMISSION_KV_POOL_TOKENS", "1.6M")
    with pytest.raises(ValueError):
        kv_budget.setting_pool_tokens()
    monkeypatch.setenv("ADMISSION_KV_RESERVE_FRACTION", "0.5")
    assert kv_budget.reserve_fraction() == 0.5
    # config.py's attribute, when it exists, wins over the environment.
    monkeypatch.setattr(settings, "admission_kv_pool_tokens", 777, raising=False)
    monkeypatch.setenv("ADMISSION_KV_POOL_TOKENS", "123")
    assert kv_budget.setting_pool_tokens() == 777
    assert kv_budget._s_str("admission_kv_metrics_url", "ADMISSION_KV_METRICS_URL", "") == ""
