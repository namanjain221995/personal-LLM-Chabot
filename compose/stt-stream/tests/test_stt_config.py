"""STT_* validation: an engine started on a typo serves nobody, so it refuses."""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import types

import pytest

from conftest import TOKEN, server

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_FILE = os.path.join(os.path.dirname(ENGINE_DIR), "compose.stt-stream.yaml")


@pytest.fixture
def model_dir(tmp_path):
    for name in server.MODEL_FILES:
        (tmp_path / name).write_bytes(b"x")
    return str(tmp_path)


def env_for(model_dir, **extra):
    profiles = [{"id": "fast", "dir": model_dir, "chunk_ms": 160, "max_streams": 12, "languages": ["auto", "en", "hi"]}]
    values = {"STT_PROFILES": json.dumps(profiles), "STT_TOKEN": TOKEN, "STT_BIND": "192.0.2.10"}
    values.update(extra)
    return values


def test_a_complete_environment_loads_with_the_spec_defaults(model_dir):
    settings = server.Settings.from_env(env_for(model_dir))
    assert (settings.port, settings.workers, settings.threads) == (30009, 4, 2)
    assert (settings.endpoint_s, settings.max_utterance_s, settings.idle_s) == (0.6, 30.0, 60.0)
    assert (settings.meeting_endpoint_s, settings.meeting_max_utterance_s) == (0.9, 25.0)
    assert (settings.max_cost, settings.renew_silence_s, settings.renew_seed_s) == (32, 2.0, 2.0)
    assert settings.lead_pad_ms == 160
    profile = settings.profiles[0]
    assert profile.flush_pad_ms == server.default_flush_pad_ms(160) == 800
    assert profile.model == os.path.basename(model_dir)
    assert profile.cost == 2
    assert server.default_flush_pad_ms(560) == 1520


def test_no_token_means_no_engine_unless_explicitly_allowed(model_dir):
    with pytest.raises(server.ConfigError, match="STT_TOKEN"):
        server.Settings.from_env(env_for(model_dir, STT_TOKEN=""))
    settings = server.Settings.from_env(env_for(model_dir, STT_TOKEN="", STT_ALLOW_NO_TOKEN="1"))
    assert settings.token == ""
    with pytest.raises(server.ConfigError, match="STT_TOKEN"):
        server.Settings.from_env(env_for(model_dir, STT_TOKEN="", STT_ALLOW_NO_TOKEN="true"))
    with pytest.raises(server.ConfigError, match="shorter"):
        server.Settings.from_env(env_for(model_dir, STT_TOKEN="short"))


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "localhost", ""])
def test_the_bind_is_one_literal_address(model_dir, bind):
    with pytest.raises(server.ConfigError, match="STT_BIND"):
        server.Settings.from_env(env_for(model_dir, STT_BIND=bind))


@pytest.mark.parametrize("key,value", [
    ("STT_WORKERS", "0"), ("STT_WORKERS", "x"), ("STT_THREADS", "99"), ("STT_ENDPOINT_S", "nan"),
    ("STT_MAX_UTTERANCE_S", "1"), ("STT_IDLE_S", "-1"), ("STT_PORT", "80"), ("STT_LEAD_PAD_MS", "5000"),
    ("STT_RESUME_MAX_S", "inf"), ("STT_MAX_COST", "0"), ("STT_MAX_COST", "1.5"), ("STT_RENEW_SILENCE_S", "-1"),
    ("STT_RENEW_SILENCE_S", "nan"), ("STT_MEETING_ENDPOINT_S", "nan"), ("STT_MEETING_MAX_UTTERANCE_S", "1"),
])
def test_numbers_out_of_range_refuse_to_start(model_dir, key, value):
    with pytest.raises(server.ConfigError, match=key):
        server.Settings.from_env(env_for(model_dir, **{key: value}))


@pytest.mark.parametrize("mutate,problem", [
    (lambda p: "not json", "valid JSON"),
    (lambda p: {"id": "fast"}, "JSON list"),
    (lambda p: [], "JSON list"),
    (lambda p: [dict(p, extra=1)], "unknown keys"),
    (lambda p: [dict(p, id="Fast One")], "'id'"),
    (lambda p: [p, dict(p)], "duplicate id"),
    (lambda p: [dict(p, dir="models/fast")], "absolute path"),
    (lambda p: [dict(p, dir="/nonexistent/stt")], "missing encoder.int8.onnx"),
    (lambda p: [dict(p, chunk_ms=10)], "chunk_ms"),
    (lambda p: [dict(p, max_streams=True)], "max_streams"),
    (lambda p: [dict(p, languages=[])], "languages"),
    (lambda p: [dict(p, languages=["auto", "english"])], "languages"),
    (lambda p: [dict(p, languages=["en", "EN"])], "twice"),
    (lambda p: [dict(p, flush_pad_ms=-1)], "flush_pad_ms"),
    (lambda p: [dict(p, model="../etc")], "'model'"),
    (lambda p: [dict(p, cost=0)], "'cost'"),
    (lambda p: [dict(p, cost=65)], "'cost'"),
    (lambda p: [dict(p, cost=True)], "'cost'"),
    (lambda p: [dict(p, cost=2.5)], "'cost'"),
])
def test_profiles_are_validated_strictly(model_dir, mutate, problem):
    base = {"id": "fast", "dir": model_dir, "chunk_ms": 160, "max_streams": 12, "languages": ["auto", "en"]}
    mutated = mutate(base)
    raw = mutated if isinstance(mutated, str) else json.dumps(mutated)
    with pytest.raises(server.ConfigError, match=re.escape(problem)):
        server.Settings.from_env(env_for(model_dir, STT_PROFILES=raw))


def test_a_meeting_endpoint_shorter_than_the_recognizers_is_refused(model_dir):
    # The recognizer's rule 2 fires first for every stream; a meeting stream
    # can only wait longer than that.
    with pytest.raises(server.ConfigError, match="STT_MEETING_ENDPOINT_S"):
        server.Settings.from_env(env_for(model_dir, STT_ENDPOINT_S="0.8", STT_MEETING_ENDPOINT_S="0.7"))
    settings = server.Settings.from_env(env_for(model_dir, STT_ENDPOINT_S="0.8", STT_MEETING_ENDPOINT_S="0.8"))
    assert settings.meeting_endpoint_s == settings.endpoint_s == 0.8


def test_a_profile_that_costs_more_than_the_whole_budget_is_refused(model_dir):
    # It could never admit a stream: a typo, not a configuration.
    profiles = [{"id": "fast", "dir": model_dir, "chunk_ms": 160, "max_streams": 4, "languages": ["en"], "cost": 9}]
    with pytest.raises(server.ConfigError, match="STT_MAX_COST"):
        server.Settings.from_env(env_for(model_dir, STT_PROFILES=json.dumps(profiles), STT_MAX_COST="8"))
    settings = server.Settings.from_env(env_for(model_dir, STT_PROFILES=json.dumps(profiles), STT_MAX_COST="9"))
    assert settings.profiles[0].cost == 9


def test_a_profile_costs_what_its_chunk_size_was_measured_to_cost():
    # 160 ms: 0.41 core a stream; 560 ms: half that (build spec section 7).
    assert (server.default_cost(160), server.default_cost(560)) == (2, 1)
    assert (server.default_cost(80), server.default_cost(1120)) == (4, 1)
    profile = server.Profile("x", "/models/x", 160, 4, ("en",), 800, "m")
    assert profile.cost == 2  # derived when not given


def test_the_real_recognizer_is_built_with_rule_3_out_of_reach(monkeypatch):
    """build_sherpa_recognizer against a stand-in sherpa_onnx: what the
    deployed engine asks of the real recognizer. sherpa-onnx counts rule 3
    from the stream's creation and nothing resets it, so a rule 3 inside a
    session would hold the endpoint for the rest of it (review finding: the
    old 3600 s was one hour of dictation)."""
    captured = {}

    class OnlineRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            captured.update(kwargs)
            return "a recognizer"

    monkeypatch.setitem(sys.modules, "sherpa_onnx", types.SimpleNamespace(OnlineRecognizer=OnlineRecognizer))
    from conftest import make_profile, make_settings

    settings = make_settings(endpoint_s=0.7, threads=3)
    assert server.build_sherpa_recognizer(make_profile("fast"), settings) == "a recognizer"
    assert captured["enable_endpoint_detection"] is True
    assert captured["rule1_min_trailing_silence"] == 2.4
    assert captured["rule2_min_trailing_silence"] == 0.7
    assert captured["rule3_min_utterance_length"] >= 100 * 24 * 3600  # 100 days of one stream
    assert captured["num_threads"] == 3 and captured["provider"] == "cpu"
    assert captured["encoder"] == "/models/fast/encoder.int8.onnx"


def test_profiles_are_required(model_dir):
    values = env_for(model_dir)
    del values["STT_PROFILES"]
    with pytest.raises(server.ConfigError, match="STT_PROFILES"):
        server.Settings.from_env(values)


def test_main_refuses_to_start_without_a_token_and_says_why(monkeypatch, caplog, model_dir):
    for key, value in env_for(model_dir, STT_TOKEN="").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("STT_ALLOW_NO_TOKEN", raising=False)
    with caplog.at_level(logging.ERROR, logger="stt-stream"), pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2
    assert "refusing to start" in caplog.text and "STT_TOKEN" in caplog.text


def test_a_profile_whose_chunk_is_not_its_models_never_becomes_ready():
    # The decoder's position is counted in steps of the profile's chunk, so a
    # 160 ms profile pointed at a 560 ms export must refuse, not mistime.
    from conftest import make_profile, make_settings, running
    from stt_fakes import FakeRecognizer

    def wrong_export(profile, settings):
        return FakeRecognizer(chunk_ms=560)

    with running(make_settings(profiles=(make_profile("fast", 160),)), wrong_export, ready=False) as client:
        import time
        deadline = time.monotonic() + 5
        while client.get("/health").json()["error"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        body = client.get("/health").json()
    assert body["ready"] is False
    assert body["error"] == "profile 'fast' says chunk_ms 160 but its model decodes 560 ms per step"


# -- the deployed configuration ---------------------------------------------------

def _compose_service():
    yaml = pytest.importorskip("yaml")
    with open(COMPOSE_FILE, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return document, document["services"]["stt-stream"]


def test_the_compose_files_profiles_are_the_amended_four_in_admission_order():
    """The JSON in compose.stt-stream.yaml is the production configuration;
    a typo in it would only show up as a restart loop on the worker."""
    _, service = _compose_service()
    profiles = server.parse_profiles(service["environment"]["STT_PROFILES"], check_files=False)
    assert [(p.id, p.chunk_ms, p.max_streams, p.languages, p.cost) for p in profiles] == [
        ("en-fast", 160, 8, ("en",), 2),
        ("multi-fast", 160, 8, ("auto", "hi", "en"), 2),
        ("en-wide", 560, 12, ("en",), 1),
        ("multi-wide", 560, 12, ("auto", "hi", "en"), 1),
    ]
    # The costs are the measured ones, written out, not left to the default.
    assert all(f'"cost": {p.cost}' in service["environment"]["STT_PROFILES"] for p in profiles)
    engine = server.Engine(server.Settings(profiles=profiles, token=TOKEN), lambda p, s: None)
    first_choice = {language: engine.admit(language)[0] for language in ("auto", "en", "hi", "gu")}
    assert {k: (v.profile.id if v else None) for k, v in first_choice.items()} == {
        "auto": "multi-fast", "en": "en-fast", "hi": "multi-fast", "gu": None}
    # Every profile directory is mounted, read-only, and nothing else is.
    mounts = {volume["target"]: volume for volume in service["volumes"]}
    assert set(mounts) == {p.dir for p in profiles}
    assert all(volume["read_only"] is True for volume in mounts.values())
    assert len({p.model for p in profiles}) == 4


def test_the_compose_file_keeps_the_placement_promises():
    document, service = _compose_service()
    environment = service["environment"]
    assert document["name"] == "sf-local-ai-stt"
    assert service["network_mode"] == "host"
    assert service["oom_score_adj"] == 900
    assert service["cpuset"] == "5-9,15-19" and service["cpus"] == 8
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "${STT_BIND:?" in environment["STT_BIND"]
    assert "STT_TOKEN" not in environment  # it comes from the 0600 env file only
    assert service["env_file"] == [{"path": "./stt-stream/stt.env", "required": True}]
    assert "STT_ALLOW_NO_TOKEN" not in json.dumps(service)
    # The decode threads of all four profiles fit the container's cores
    # (build spec 8E): profiles x STT_WORKERS <= cpus.
    workers = int(re.search(r":-(\d+)\}", environment["STT_WORKERS"]).group(1))
    profiles = server.parse_profiles(environment["STT_PROFILES"], check_files=False)
    assert len(profiles) * workers <= service["cpus"]
    # THE BUDGET BINDS: the profiles' slots would take 56 units, and 32 is
    # the 16 real-time 160 ms streams the 8 cores decode (0.41 core each).
    budget = int(re.search(r":-(\d+)\}", environment["STT_MAX_COST"]).group(1))
    assert budget == 32 < sum(p.max_streams * p.cost for p in profiles)
    assert budget // 2 * 0.41 <= service["cpus"] - 1.0
    # A hard memory limit, measured (3.58 GB loaded, 4.08 GB with 12
    # streams): the one deliberate exception test_oom_score_adj.py allows.
    assert service["mem_limit"] == "8g"
    # The renewal threshold the compose file passes is the engine's own
    # default, the measured one (Settings.renew_silence_s).
    renew = float(re.search(r":-([0-9.]+)\}", environment["STT_RENEW_SILENCE_S"]).group(1))
    assert renew == server.Settings(profiles=profiles, token=TOKEN).renew_silence_s == 2.0
