"""Config reads the compose env var names (MAIN_MODEL, RERANKER_MODEL, and
the vLLM-design sidecar vars ROUTER/VISION/EMBED_BASE_URL + model names)."""
from app.config import Settings

_SIDECAR_ENV = (
    "ROUTER_BASE_URL",
    "VISION_BASE_URL",
    "EMBED_BASE_URL",
    "ROUTER_MODEL",
    "VISION_MODEL",
    "EMBED_MODEL",
)


def test_main_model_default_matches_spec(monkeypatch):
    monkeypatch.delenv("MAIN_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert Settings().llm_model == "Qwen/Qwen3.6-35B-A3B-NVFP4"


def test_main_model_env_is_honored(monkeypatch):
    monkeypatch.setenv("MAIN_MODEL", "openai/gpt-oss-20b")
    assert Settings().llm_model == "openai/gpt-oss-20b"


def test_reranker_model_default_matches_spec(monkeypatch):
    monkeypatch.delenv("RERANKER_MODEL", raising=False)
    monkeypatch.delenv("RERANK_MODEL", raising=False)
    assert Settings().rerank_model == "Qwen/Qwen3-Reranker-0.6B"


def test_reranker_model_env_is_honored(monkeypatch):
    monkeypatch.setenv("RERANKER_MODEL", "Qwen/Qwen3-Reranker-4B")
    assert Settings().rerank_model == "Qwen/Qwen3-Reranker-4B"


def test_vllm_sidecar_defaults_match_design(monkeypatch):
    for name in _SIDECAR_ENV:
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    # Classification + agent sub-steps run on the small model again.
    assert s.router_base_url == "http://vllm-router:30002/v1"
    assert s.agent_base_url == s.router_base_url
    assert s.router_model == "Qwen/Qwen3-VL-8B-Instruct-FP8"
    assert s.vision_base_url == "http://vllm:30000/v1"
    assert s.vision_model == "Qwen/Qwen3.6-35B-A3B-NVFP4"
    assert s.embed_base_url == "http://vllm-embed:30003/v1"
    assert s.embed_model == "Qwen/Qwen3-Embedding-0.6B"


def test_vllm_sidecar_env_overrides_and_trailing_slash(monkeypatch):
    monkeypatch.setenv("ROUTER_BASE_URL", "http://localhost:8002/v1/")
    monkeypatch.setenv("EMBED_MODEL", "custom-embed")
    s = Settings()
    assert s.router_base_url == "http://localhost:8002/v1"
    assert s.embed_model == "custom-embed"


def test_openai_base_url_default_is_vllm_service(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert Settings().openai_base_url == "http://vllm:30000/v1"


def test_cors_origins_default_is_local_frontend_only(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    origins = Settings().cors_allow_origins
    assert "*" not in origins
    assert "http://localhost:3000" in origins


def test_cors_origins_env_override(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://example.local:3000")
    assert Settings().cors_allow_origins == ["http://example.local:3000"]


# --- CHART_TRIGGER_MODE -----------------------------------------------------


def test_chart_trigger_mode_defaults_to_explicit(monkeypatch):
    """The default must preserve today's behaviour exactly: a chart appears
    only when the user asked for one."""
    from app.config import Settings

    monkeypatch.delenv("CHART_TRIGGER_MODE", raising=False)
    assert Settings().chart_trigger_mode == "explicit"


def test_chart_trigger_mode_accepts_hybrid(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("CHART_TRIGGER_MODE", "Hybrid")
    assert Settings().chart_trigger_mode == "hybrid"


def test_an_unknown_chart_trigger_mode_falls_back_to_explicit(monkeypatch):
    """Including `automatic`, which is deliberately not implemented. The
    failure mode of guessing is charts appearing where nobody wanted them."""
    from app.config import Settings

    for value in ("automatic", "", "  ", "yes-please"):
        monkeypatch.setenv("CHART_TRIGGER_MODE", value)
        assert Settings().chart_trigger_mode == "explicit"


def test_sf_live_enabled_off_means_off(monkeypatch):
    """The old bespoke parse treated 'off' (and typos) as TRUE, silently
    enabling live org lookups. It now uses the shared _bool helper."""
    from app.config import Settings

    for raw in ("off", "false", "0", "no", "nonsense"):
        monkeypatch.setenv("SF_LIVE_ENABLED", raw)
        assert Settings().sf_live_enabled is False, raw
    for raw in ("true", "1", "yes", "on"):
        monkeypatch.setenv("SF_LIVE_ENABLED", raw)
        assert Settings().sf_live_enabled is True, raw
    monkeypatch.delenv("SF_LIVE_ENABLED", raising=False)
    assert Settings().sf_live_enabled is True
