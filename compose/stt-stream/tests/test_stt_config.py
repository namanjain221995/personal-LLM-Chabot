"""STT_* validation: an engine started on a typo serves nobody, so it refuses."""
from __future__ import annotations

import json
import logging
import os
import re

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
    assert settings.lead_pad_ms == 160
    profile = settings.profiles[0]
    assert profile.flush_pad_ms == server.default_flush_pad_ms(160) == 800
    assert profile.model == os.path.basename(model_dir)
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
    ("STT_RESUME_MAX_S", "inf"),
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
])
def test_profiles_are_validated_strictly(model_dir, mutate, problem):
    base = {"id": "fast", "dir": model_dir, "chunk_ms": 160, "max_streams": 12, "languages": ["auto", "en"]}
    mutated = mutate(base)
    raw = mutated if isinstance(mutated, str) else json.dumps(mutated)
    with pytest.raises(server.ConfigError, match=re.escape(problem)):
        server.Settings.from_env(env_for(model_dir, STT_PROFILES=raw))


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
    assert [(p.id, p.chunk_ms, p.max_streams, p.languages) for p in profiles] == [
        ("en-fast", 160, 8, ("en",)),
        ("multi-fast", 160, 8, ("auto", "hi", "en")),
        ("en-wide", 560, 12, ("en",)),
        ("multi-wide", 560, 12, ("auto", "hi", "en")),
    ]
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
