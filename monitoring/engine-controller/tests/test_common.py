"""Unit tests for the shared helpers: the vLLM metrics parser, Docker
timestamps, the multiplexed log stream, the exposition renderer, the budget."""
from __future__ import annotations

import os

import pytest

from common import Budget, MetricsDoc, demux_docker_stream, parse_docker_time, parse_vllm_metrics
from fakes import FakeClock

REAL_METRICS = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4"} 9.0
vllm:num_requests_waiting{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4"} 2.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4",reason="capacity"} 7.0
vllm:num_requests_waiting_by_reason{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4",reason="deferred"} 0.0
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4"} 624271.0
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4"} 2138504.0
vllm:generation_tokens_total{engine="1",model_name="Qwen/Qwen3.6-35B-A3B-NVFP4"} 100.0
process_cpu_seconds_total 12.5
"""


def test_parse_vllm_metrics_with_model_name_label_sums_engines_and_matches_exact_names():
    parsed = parse_vllm_metrics(REAL_METRICS)
    assert parsed["requests_running"] == 9.0
    # the _by_reason sibling must NOT fold into num_requests_waiting
    assert parsed["requests_waiting"] == 2.0
    assert parsed["prompt_tokens_total"] == 624271.0
    # two engines are summed
    assert parsed["generation_tokens_total"] == 2138504.0 + 100.0


def test_parse_vllm_metrics_ignores_series_without_model_name_and_reports_absence():
    text = "vllm:num_requests_running 5\nvllm:generation_tokens_total{engine=\"0\"} 5\n"
    parsed = parse_vllm_metrics(text)
    assert parsed["requests_running"] is None
    assert parsed["generation_tokens_total"] is None
    assert parsed["prompt_tokens_total"] is None


def test_parse_docker_time_handles_nanoseconds_and_zero_value():
    at = parse_docker_time("2026-09-11T22:22:47.688511544Z")
    assert at is not None and abs(at - 1789165367.688511) < 0.001
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("") is None
    assert parse_docker_time(None) is None
    assert parse_docker_time("garbage") is None


def test_demux_docker_stream_reassembles_frames_and_passes_raw_text_through():
    payload = b"2026-09-11T22:15:50.297Z line one\n"
    frame = bytes([1, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
    frame2 = bytes([2, 0, 0, 0]) + (5).to_bytes(4, "big") + b"err!\n"
    assert demux_docker_stream(frame + frame2) == payload.decode() + "err!\n"
    assert demux_docker_stream(b"plain tty output\n") == "plain tty output\n"


def test_metrics_doc_one_hot_is_bounded_and_help_is_written_once():
    d = MetricsDoc()
    d.one_hot("x_state", "help", "state", ("A", "B"), "B")
    d.counter("x_total", 3, "help", {"outcome": "ok"})
    d.counter("x_total", 4, "help", {"outcome": "err"})
    text = d.render()
    assert 'x_state{state="A"} 0' in text and 'x_state{state="B"} 1' in text
    assert text.count("# HELP x_total") == 1 and text.count("# TYPE x_total counter") == 1
    with pytest.raises(ValueError):
        d.one_hot("x_state2", "help", "state", ("A", "B"), "C")


def test_budget_slides():
    clock = FakeClock()
    b = Budget(2, 100.0, clock)
    assert not b.exhausted() and b.remaining() == 2
    b.record()
    b.record()
    assert b.exhausted() and b.remaining() == 0
    clock.advance(101)
    assert not b.exhausted() and b.used() == 0


def test_parse_gpu_utilization_takes_the_maximum_and_ignores_longer_names():
    from common import parse_gpu_utilization

    text = ('dgx_gpu_up 1\n'
            'dgx_gpu_utilization_percent{gpu="0",uuid="GPU-a",name="NVIDIA GB10"} 93.0\n'
            'dgx_gpu_utilization_percent{gpu="1",uuid="GPU-b",name="NVIDIA GB10"} 12.0\n'
            'dgx_gpu_utilization_percent_other{gpu="0"} 100.0\n'
            'dgx_gpu_memory_controller_utilization_percent{gpu="0"} 99.0\n')
    assert parse_gpu_utilization(text) == 93.0
    assert parse_gpu_utilization("dgx_gpu_up 0\n") is None          # nvidia-smi did not answer: no reading
    assert parse_gpu_utilization("dgx_gpu_utilization_percent 41\n") == 41.0
    assert parse_gpu_utilization(text, "dgx_gpu_memory_controller_utilization_percent") == 99.0


def test_scan_compile_error_lines_needs_an_error_marker_and_returns_only_the_bounded_signature():
    from common import COMPILE_SIGNATURES, scan_compile_error_lines

    assert scan_compile_error_lines("INFO Using nvcc from /usr/local/cuda/bin/nvcc\n") is None
    assert scan_compile_error_lines("ERROR flashinfer.jit: compilation failed at /root/.cache/flashinfer/x.cu\n") == "flashinfer.jit"
    assert scan_compile_error_lines("INFO torch._dynamo.config.cache_size_limit = 64\n") is None
    assert scan_compile_error_lines("ERROR torch._dynamo.exc.BackendCompilerFailed: inductor\n") == "torch._dynamo"
    assert scan_compile_error_lines("cuda_nvrtc: NVRTC_ERROR_COMPILATION failed\n") == "cuda_nvrtc"
    assert set(COMPILE_SIGNATURES) == {"torch._dynamo", "flashinfer.jit", "nvcc", "cuda_nvrtc"}


def test_error_kinds_are_bounded_and_never_carry_a_path_or_address(tmp_path):
    from common import DOCKER_UNAVAILABLE_KINDS, ConnectFailed, DockerClient, DockerError, DockerUnavailable, fetch

    missing = DockerClient(str(tmp_path / "no.sock"), timeout=1.0)
    with pytest.raises(DockerUnavailable) as info:
        missing.inspect("x")
    assert info.value.kind == "socket_missing" and info.value.kind in DOCKER_UNAVAILABLE_KINDS
    assert "FileNotFoundError" in str(info.value)     # the log line keeps the detail…
    assert DockerError(409, "container x is not running").kind == "api_409"
    assert DockerUnavailable("weird", "not-a-kind").kind == "broken"
    with pytest.raises(ConnectFailed) as info:
        fetch("http://127.0.0.1:1/health", connect_timeout=0.5, read_timeout=0.5)
    assert info.value.kind == "refused" and info.value.refused is True
    assert ConnectFailed("x", refused=False).kind == "broken"


def test_parse_meminfo_available_reads_the_kernel_shape_and_reports_absence(tmp_path):
    from common import parse_meminfo_available, read_mem_available
    from fakes import meminfo_text

    # the kernel's exact shape: right-aligned kB rows, MemAvailable after MemTotal/MemFree
    text = meminfo_text(45.3)
    assert parse_meminfo_available(text) == int(45.3 * 1024 * 1024) * 1024
    assert parse_meminfo_available("MemTotal:       127600812 kB\nMemFree:  33000000 kB\n") is None
    assert parse_meminfo_available("MemAvailable:   not-a-number kB\n") is None
    assert parse_meminfo_available("MemAvailable:\n") is None
    assert parse_meminfo_available("MemAvailable: 12 parsecs\n") is None
    assert parse_meminfo_available("MemAvailable: 4096\n") == 4096       # unitless = bytes
    assert parse_meminfo_available("MemAvailable: -1 kB\n") is None
    assert parse_meminfo_available("MemAvailableX: 5 kB\nMemAvailable: 5 kB\n") == 5 * 1024
    # the file reader never raises: missing → None
    assert read_mem_available(str(tmp_path / "missing")) is None
    path = tmp_path / "meminfo"
    path.write_text(text, encoding="ascii")
    assert read_mem_available(str(path)) == int(45.3 * 1024 * 1024) * 1024


@pytest.mark.skipif(not os.path.exists("/proc/meminfo"), reason="no procfs")
def test_read_mem_available_agrees_with_the_real_proc_meminfo():
    """The controller reads the HOST's /proc/meminfo (host network, no
    lxcfs — verified 2026-09-12 against `free -b` on this host): the parser
    must agree with a direct read of the live row."""
    from common import read_mem_available

    value = read_mem_available()
    assert value is not None and value > 0
    with open("/proc/meminfo", encoding="ascii") as fh:
        row = next(line for line in fh if line.startswith("MemAvailable:"))
    live_kb = int(row.split()[1])
    # the kernel's estimate moves between the two reads; a few hundred MB either way
    assert abs(value - live_kb * 1024) < 2 * 1024 ** 3
