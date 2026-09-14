"""The no-timeout /v1 settings (design revision 2, 2026-09-13).

What is pinned: every PUBLIC_API_* detector threshold and physical guard the
design names exists with the design's default and follows its environment
variable; the retired wall-clock and wait-bound settings are ignored with ONE
warning each per process; and no setting that the sidecar team's code reads
with a different default is defined here (registry.setting_* reads Settings
before the environment, so defining one would change that code's behaviour
before its validation lands).
"""
from __future__ import annotations

import logging

import pytest

from app import config
from app.config import RETIRED_PUBLIC_API_SETTINGS, Settings

#: (attribute, environment variable, design default, an override to try)
DESIGN_DEFAULTS = [
    ("public_api_liveness_quiet_s", "PUBLIC_API_LIVENESS_QUIET_S", 120.0, "90"),
    ("public_api_liveness_not_serving_s", "PUBLIC_API_LIVENESS_NOT_SERVING_S", 30.0, "45"),
    ("public_api_liveness_lost_min_s", "PUBLIC_API_LIVENESS_LOST_MIN_S", 300.0, "600"),
    ("public_api_liveness_unknown_silence_s", "PUBLIC_API_LIVENESS_UNKNOWN_SILENCE_S", 3600.0, "7200"),
    ("public_api_engine_down_grace_s", "PUBLIC_API_ENGINE_DOWN_GRACE_S", 1800.0, "900"),
    ("public_api_resume_max_stalled_attempts", "PUBLIC_API_RESUME_MAX_STALLED_ATTEMPTS", 3, "5"),
    ("public_api_quarantine_serving_s", "PUBLIC_API_QUARANTINE_SERVING_S", 300.0, "600"),
    ("public_api_quarantine_min_recoveries", "PUBLIC_API_QUARANTINE_MIN_RECOVERIES", 2, "1"),
    ("public_api_resume_enabled", "PUBLIC_API_RESUME_ENABLED", True, "false"),
    ("public_api_resume_stagger_s", "PUBLIC_API_RESUME_STAGGER_S", 30.0, "10"),
    ("public_api_suspended_unread_ttl_s", "PUBLIC_API_SUSPENDED_UNREAD_TTL_S", 900.0, "60"),
    ("public_api_stream_orphan_grace_s", "PUBLIC_API_STREAM_ORPHAN_GRACE_S", 600.0, "300"),
    ("public_api_unkeyed_orphan_grace_s", "PUBLIC_API_UNKEYED_ORPHAN_GRACE_S", 120.0, "30"),
    ("public_api_implicit_attach_window_s", "PUBLIC_API_IMPLICIT_ATTACH_WINDOW_S", 3600.0, "60"),
    ("public_api_follower_poll_s", "PUBLIC_API_FOLLOWER_POLL_S", 1.0, "2"),
    ("public_api_pending_events_max_bytes", "PUBLIC_API_PENDING_EVENTS_MAX_BYTES", 67_108_864, "1024"),
    ("public_api_event_retention_s", "PUBLIC_API_EVENT_RETENTION_S", 3600.0, "60"),
    ("public_api_sync_commit_s", "PUBLIC_API_SYNC_COMMIT_S", 12.0, "10"),
    ("public_api_blob_dir", "PUBLIC_API_BLOB_DIR", "/data/publicapi/blobs", "/tmp/blobs"),
    ("public_api_fd_guard_ratio", "PUBLIC_API_FD_GUARD_RATIO", 0.70, "0.5"),
    ("public_api_main_normal_max_concurrent", "PUBLIC_API_MAIN_NORMAL_MAX_CONCURRENT", 6, "4"),
    ("public_api_chat_normal_reserve", "PUBLIC_API_CHAT_NORMAL_RESERVE", 4, "2"),
    ("public_api_shared_kv_budget_tokens", "PUBLIC_API_SHARED_KV_BUDGET_TOKENS", 1_400_000, "0"),
    ("public_api_long_release_at_first_token", "PUBLIC_API_LONG_RELEASE_AT_FIRST_TOKEN", True, "false"),
    # T1 review fixes (2026-09-14): the decode-yield cap and the continuation
    # sizing timeout.
    ("public_api_decode_yields_per_run", "PUBLIC_API_DECODE_YIELDS_PER_RUN", 1, "0"),
    ("public_api_continuation_tokenize_timeout_s", "PUBLIC_API_CONTINUATION_TOKENIZE_TIMEOUT_S", 120.0, "300"),
    ("public_api_sidecar_silence_s", "PUBLIC_API_SIDECAR_SILENCE_S", 1800.0, "600"),
    ("public_api_pooling_silence_s", "PUBLIC_API_POOLING_SILENCE_S", 600.0, "120"),
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for _attr, env, _default, _override in DESIGN_DEFAULTS:
        monkeypatch.delenv(env, raising=False)
    for name in RETIRED_PUBLIC_API_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_retired_warned", set())
    yield


@pytest.mark.parametrize("attr,env,default,override", DESIGN_DEFAULTS)
def test_every_no_timeout_setting_has_the_design_default_and_follows_its_variable(
    monkeypatch, attr, env, default, override
):
    assert getattr(Settings(), attr) == default
    monkeypatch.setenv(env, override)
    value = getattr(Settings(), attr)
    if isinstance(default, bool):
        assert value is (override.lower() in {"1", "true", "yes", "on"})
    else:
        assert value == type(default)(override)


def test_a_blank_variable_means_the_default_not_zero(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_ENGINE_DOWN_GRACE_S", "")
    monkeypatch.setenv("PUBLIC_API_BLOB_DIR", "  ")
    s = Settings()
    assert s.public_api_engine_down_grace_s == 1800.0
    assert s.public_api_blob_dir == "/data/publicapi/blobs"


def test_the_main_normal_gate_leaves_chat_its_reserve_of_the_normal_lane():
    s = Settings()
    assert s.public_api_main_normal_max_concurrent + s.public_api_chat_normal_reserve == s.admission_normal_max


def test_settings_whose_first_readers_belong_to_other_modules_are_not_defined_here():
    """registry.setting_* and disk_ledger read Settings before the environment:
    defining these would override those modules' own defaults and their
    run-time environment reads."""
    s = Settings()
    # PUBLIC_API_EMBED_MAX_INPUTS and PUBLIC_API_RERANK_MAX_DOCUMENTS were
    # declared by PR #65 (the six-model integration) with the readers' own
    # defaults, pinned by tests/test_public_api_settings_declared.py; merged
    # 2026-09-14, so only these two remain the other modules' to define.
    for attr in ("public_api_min_free_disk_bytes", "public_api_decode_concurrency"):
        assert not hasattr(s, attr), attr


def test_each_retired_setting_is_warned_about_once_and_never_defined(monkeypatch, caplog):
    monkeypatch.setenv("PUBLIC_API_GEN_WALL_CLOCK_S", "21600")
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "30")
    monkeypatch.setenv("PUBLIC_API_MAX_AUDIO_SECONDS", "  ")  # blank: not set
    with caplog.at_level(logging.WARNING, logger="app.config"):
        first = config.warn_retired_settings()
        second = config.warn_retired_settings()
        Settings()  # constructing settings again does not repeat them either
    assert sorted(first) == ["PUBLIC_API_GATE_WAIT_S", "PUBLIC_API_GEN_WALL_CLOCK_S"]
    assert second == []
    lines = [r.getMessage() for r in caplog.records if "retired" in r.getMessage()]
    assert len(lines) == 2
    # Assembler, 2026-09-14: both still have a reader in this build, so the
    # warning names it rather than calling the setting ignored.
    assert all("still reads it in" in line and "ignored" not in line for line in lines)
    monkeypatch.setattr(config, "_retired_warned", set())
    with caplog.at_level(logging.WARNING, logger="app.config"):
        caplog.clear()
        assert config.warn_retired_settings({"PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S": "120"}) == [
            "PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S"
        ]
    assert any("is ignored" in r.getMessage() for r in caplog.records)
    s = Settings()
    for name in ("PUBLIC_API_GEN_WALL_CLOCK_S", "PUBLIC_API_GATE_WAIT_S", "PUBLIC_API_BACKGROUND_GATE_WAIT_S",
                 "PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S", "PUBLIC_API_MAX_AUDIO_SECONDS"):
        assert not hasattr(s, name.lower()), f"{name} is retired and must not be a setting"


def test_warn_retired_settings_reads_a_given_environment_and_never_raises():
    assert config.warn_retired_settings({"PUBLIC_API_BACKGROUND_GATE_WAIT_S": "3600"}) == [
        "PUBLIC_API_BACKGROUND_GATE_WAIT_S"
    ]
    assert config.warn_retired_settings({}) == []


def test_the_still_read_list_matches_the_code_that_reads_each_retired_setting():
    """Assembler, 2026-09-14: a retired setting is warned about as "ignored"
    only when nothing outside config.py reads it, and as "still read" only
    while something does — so the list cannot go stale in either direction."""
    import pathlib
    import re

    app_dir = pathlib.Path(config.__file__).resolve().parent
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in app_dir.rglob("*.py")
        if path.name != "config.py"
    }

    def readers(name: str) -> list:
        quoted = re.compile(r"[\"']" + re.escape(name) + r"[\"']")
        attribute = re.compile(r"\bsettings\." + re.escape(name.lower()) + r"\b")
        return [str(p) for p, text in sources.items() if quoted.search(text) or attribute.search(text)]

    for name in RETIRED_PUBLIC_API_SETTINGS:
        found = readers(name)
        if name in config.STILL_READ_RETIRED_SETTINGS:
            assert found, f"{name} is listed as still read, but nothing reads it: delete its entry"
        else:
            assert not found, f"{name} is warned about as ignored, but {found} read it"
