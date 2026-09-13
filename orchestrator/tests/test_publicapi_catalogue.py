"""The six-model public catalogue (CONTRACT §15, owner request 2026-09-13).

Offline: the registry reads `settings` and nothing else, so every assertion
here is about what `/v1/models` would say on a deployment configured a given
way — and about the two things the catalogue may never do, whatever the
configuration: publish an internal identity, or let a database row widen it.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest

from app import context
from app.config import settings
from app.model_capabilities import RerankerBackend
from app.publicapi import engines, registry


@pytest.fixture(autouse=True)
def _no_served_windows():
    engines.reset_probe_state()
    yield
    engines.reset_probe_state()


def _ids(models):
    return [model.id for model in models]


# ------------------------------------------------------- what is declared --


def test_every_engine_this_deployment_runs_is_declared_and_nothing_else(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "rerank_base_url", "http://vllm-reranker:30005")
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", ("http://10.0.0.8:30007/v1",))

    assert _ids(registry.declared_models()) == list(registry.PUBLIC_MODEL_IDS)


def test_an_engine_the_deployment_does_not_run_is_not_configured_rather_than_listed(monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.INPROCESS)
    monkeypatch.setattr(settings, "asr_enabled", False)

    declared = _ids(registry.declared_models())
    assert "techsara-ocr" not in declared
    assert "techsara-rerank" not in declared
    assert "techsara-whisper" not in declared
    statuses = {model.id: model.status for model in registry.catalogue()}
    # The console still shows all six, so an operator can see what is off.
    assert list(statuses) == list(registry.PUBLIC_MODEL_IDS)
    assert statuses["techsara-ocr"] == statuses["techsara-rerank"] == "not_configured"
    assert statuses["techsara-whisper"] == "not_configured"
    assert statuses["techsara-35b"] == "available"
    # A not-configured entry names nothing that could be reached.
    for model in registry.catalogue():
        if model.status == "not_configured":
            assert model.internal == ""


def test_a_router_pointed_at_the_main_engine_is_withdrawn_so_the_main_model_has_one_id(monkeypatch):
    # A profile that points ROUTER_BASE_URL at the main engine would otherwise
    # publish the main model a second time, around its breaker and lanes.
    monkeypatch.setattr(settings, "router_base_url", settings.openai_base_url)
    assert "techsara-8b-vision" not in _ids(registry.declared_models())

    monkeypatch.setattr(settings, "router_base_url", "http://vllm-router:30002/v1")
    monkeypatch.setattr(settings, "router_model", settings.llm_model)
    assert "techsara-8b-vision" not in _ids(registry.declared_models())


def test_a_misconfigured_sidecar_is_withdrawn_without_taking_the_flagship_down(monkeypatch):
    # OCR_MODEL pasted as the embeddings model's name: the guard refuses to
    # publish another engine's checkpoint under the OCR id, and the flagship
    # keeps serving.
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "ocr_model", settings.embed_model)

    declared = _ids(registry.declared_models())
    assert "techsara-ocr" not in declared
    assert declared[0] == "techsara-35b"


def test_a_database_row_cannot_add_a_model_the_deployment_does_not_run(monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", False)
    assert registry.resolve_public_model("techsara-whisper", overrides={"techsara-whisper": True}) is None


def test_an_allowlist_that_names_only_the_flagship_hides_the_other_models():
    assert registry.resolve_public_model("techsara-8b-vision", allowed=["techsara-35b"]) is None
    assert registry.resolve_public_model("techsara-8b-vision", allowed=[]) is not None


# --------------------------------------------------------------- ceilings --


def test_the_flagship_offers_one_million_output_tokens_and_keeps_the_eight_thousand_default(monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.delenv("PUBLIC_API_MAX_OUTPUT_TOKENS", raising=False)
    model = registry.resolve_public_model("techsara-35b")

    assert model.max_output_tokens == 1_000_000
    assert model.default_max_output_tokens == 8192
    assert model.context_window == 1_000_000
    assert model.max_input_tokens == 1_000_000 - settings.context_safety_margin - 256


def test_the_output_ceiling_is_the_public_setting_and_never_the_chat_apps_model_max_output(monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "model_max_output", 8192)
    # The environment path: config.py declares the attribute since the
    # 2026-09-13 integration, and a declared attribute wins over the variable.
    monkeypatch.delattr(settings, "public_api_max_output_tokens", raising=False)
    monkeypatch.setenv("PUBLIC_API_MAX_OUTPUT_TOKENS", "300000")
    assert registry.resolve_public_model("techsara-35b").max_output_tokens == 300_000
    # config.py's rule: blank means the default.
    monkeypatch.setenv("PUBLIC_API_MAX_OUTPUT_TOKENS", " ")
    assert registry.resolve_public_model("techsara-35b").max_output_tokens == 1_000_000
    # Once config.py names the attribute, it wins over the environment.
    monkeypatch.setattr(settings, "public_api_max_output_tokens", 500_000, raising=False)
    assert registry.resolve_public_model("techsara-35b").max_output_tokens == 500_000


def test_the_router_window_is_half_the_engines_and_never_the_wrong_env_capability(monkeypatch):
    # generated.env says ROUTER_CONTEXT_LENGTH=65536; the engine serves 49,152;
    # the public window is 24,576 so chat's per-turn routing keeps its pool.
    model = registry.resolve_public_model("techsara-8b-vision")
    assert model.context_window == 24_576
    assert model.max_output_tokens == 24_576
    assert model.default_max_output_tokens == 8192
    assert model.limits == {"max_images_per_request": 8}


def test_a_served_window_can_narrow_a_ceiling_and_never_widen_it(monkeypatch):
    registry.note_served_window("router", settings.router_base_url, 16_000)
    assert registry.resolve_public_model("techsara-8b-vision").context_window == 16_000

    registry.note_served_window("router", settings.router_base_url, 49_152)
    assert registry.resolve_public_model("techsara-8b-vision").context_window == 24_576


def test_a_served_window_measured_on_another_url_is_not_believed(monkeypatch):
    registry.note_served_window("router", "http://somewhere-else:1/v1", 1000)
    assert registry.resolve_public_model("techsara-8b-vision").context_window == 24_576


def test_the_served_window_probe_reads_max_model_len_for_the_served_model(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={"data": [{"id": settings.router_model, "max_model_len": 20_000}]},
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    import asyncio

    assert asyncio.run(engines.served_window("router")) == 20_000
    assert seen == [f"{settings.router_base_url}/models"]
    assert registry.resolve_public_model("techsara-8b-vision").context_window == 20_000


def test_a_failing_probe_leaves_the_public_number_and_is_not_retried_every_request(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    import asyncio

    assert asyncio.run(engines.served_window("ocr")) is None
    assert asyncio.run(engines.served_window("ocr")) is None
    assert len(calls) == 1
    assert registry.resolve_public_model("techsara-ocr").context_window == 8192


def test_models_that_generate_nothing_have_null_output_ceilings_never_zero(monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", True)
    embed = registry.resolve_public_model("techsara-embed").to_wire()
    whisper = registry.resolve_public_model("techsara-whisper").to_wire()

    assert embed["max_output_tokens"] is None and embed["default_max_output_tokens"] is None
    assert embed["limits"]["embedding_dimensions"] == 1024
    assert embed["endpoints"] == ["/v1/embeddings"] and embed["kind"] == "embedding"
    assert whisper["context_window"] is None and whisper["max_input_tokens"] is None
    assert whisper["limits"]["max_audio_seconds"] == 300
    assert whisper["endpoints"] == ["/v1/audio/transcriptions"]


def test_the_repeated_min_output_tokens_agrees_with_the_context_module():
    # The registry may import only ..config, so it repeats this number.
    assert registry.MIN_OUTPUT_TOKENS == context.MIN_OUTPUT_TOKENS


# ------------------------------------------------------------- the guards --


def test_a_public_id_must_be_techsara_vocabulary():
    for bad in (
        "Qwen/Qwen3-VL-8B-Instruct-FP8",
        "http://vllm-router:30002/v1",
        "vllm-router:30002",
        "techsara_35b",
        "techsara-",
        "TECHSARA-35B",
        "techsara-35b.internal",
    ):
        with pytest.raises(registry.InternalTargetError):
            registry.guard_public_id(bad)
    assert registry.guard_public_id("techsara-8b-vision") == "techsara-8b-vision"


def test_a_public_id_that_is_an_engines_hostname_is_refused(monkeypatch):
    monkeypatch.setattr(settings, "ocr_base_url", "http://techsara-ocr:30004/v1")
    with pytest.raises(registry.InternalTargetError):
        registry.guard_public_id("techsara-ocr")


def test_an_internal_target_may_never_be_an_address_in_any_spelling():
    for address in (
        settings.openai_base_url,
        settings.router_base_url,
        "http://192.0.2.68:30004/v1",
        "192.0.2.68:30004",
        "192.0.2.68",
        "vllm-router",
        "vllm",
    ):
        for engine in ("main", "router", "ocr"):
            with pytest.raises(registry.InternalTargetError):
                registry.guard_internal_target(address, engine=engine)


def test_an_engine_may_not_publish_another_engines_checkpoint():
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target(settings.embed_model, engine="ocr")
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target(settings.router_model, engine="main")
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target(settings.llm_model, engine="router")
    assert registry.guard_internal_target(settings.ocr_model, engine="ocr") == settings.ocr_model


def test_an_engine_key_outside_the_closed_set_is_refused():
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target(settings.llm_model, engine="vision")
    with pytest.raises(registry.InternalTargetError):
        registry.PublicModel(
            id="techsara-extra",
            internal=settings.llm_model,
            engine="agent",
            chat=True,
            streaming=True,
            vision=False,
            tools=False,
            embeddings=False,
            max_input_tokens=1,
            max_output_tokens=1,
        )


def test_no_rendering_of_any_catalogue_entry_names_an_engine_address_or_checkpoint(monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "rerank_base_url", "http://vllm-reranker:30005")
    text = json.dumps([model.to_wire() for model in registry.catalogue()])
    for secret in (
        settings.llm_model, settings.router_model, settings.ocr_model, settings.embed_model,
        settings.rerank_model, settings.asr_model, "vllm", "30002", "30004", "30005", "192.168",
        '"main"', '"router"', '"asr"',
    ):
        assert secret not in text, secret


def test_the_registry_still_imports_nothing_but_config():
    # The api_contract CI job imports it on a runner with no engine stack.
    source = Path(registry.__file__).read_text()
    relative = [
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level
    ]
    assert relative == ["config"]
