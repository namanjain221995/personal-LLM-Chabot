"""What an API key can reach: the eleven operations of CONTRACT §7, and nothing
that names the inside.

MOVED HERE 2026-09-13 from tests/test_publicapi_routes.py, where the exact path
set was eight. `/v1/embeddings`, `/v1/rerank` and `/v1/audio/transcriptions`
make it eleven; the CI gate's `public-api-surface.txt` must say the same.

Pure where it can be: the document is built without a database, and the route
table is read off the router object. The one served-document test mounts the
router on a bare app with no key (the schema is public).
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.apiplatform.scopes import SCOPE_DESCRIPTIONS
from app.config import settings
from app.model_capabilities import RerankerBackend
from app.publicapi import endpoints, openapi as openapi_module, router as public_router

ELEVEN = {
    "GET /v1/models",
    "GET /v1/models/{model}",
    "POST /v1/responses",
    "GET /v1/responses/{id}",
    "POST /v1/responses/{id}/cancel",
    "POST /v1/chat/completions",
    "POST /v1/embeddings",
    "POST /v1/rerank",
    "POST /v1/audio/transcriptions",
    "GET /v1/usage",
    "GET /v1/openapi.json",
}

#: The fourteen `/v1/files` and `/v1/uploads` operations (files-hookup,
#: 2026-09-13): described exactly while `router.FILES_MOUNTED`.
FILES = {
    "POST /v1/files",
    "GET /v1/files",
    "GET /v1/files/{file_id}",
    "GET /v1/files/{file_id}/content",
    "DELETE /v1/files/{file_id}",
    "GET /v1/files/{file_id}/events",
    "GET /v1/files/{file_id}/derived",
    "GET /v1/files/{file_id}/derived/{name}",
    "POST /v1/uploads",
    "GET /v1/uploads/{upload_id}",
    "POST /v1/uploads/{upload_id}/parts",
    "PUT /v1/uploads/{upload_id}/parts/{part_number}",
    "POST /v1/uploads/{upload_id}/complete",
    "POST /v1/uploads/{upload_id}/cancel",
}

#: Recognisable internal identities, installed into settings so a leak of any
#: of them into the public document is detectable.
_INTERNAL = {
    "openai_base_url": "http://main-engine.internal:30000/v1",
    "llm_model": "Qwen/Qwen3.6-35B-A3B-NVFP4",
    "router_base_url": "http://router-engine.internal:30002/v1",
    "router_model": "Qwen/Qwen3-VL-8B-Instruct-FP8",
    "embed_base_url": "http://embed-engine.internal:30003/v1",
    "embed_model": "Qwen/Qwen3-Embedding-0.6B",
    "ocr_base_url": "http://192.0.2.68:30004/v1",
    "ocr_model": "baidu/Unlimited-OCR",
    "rerank_base_url": "http://rerank-engine.internal:30005",
    "rerank_model": "Qwen/Qwen3-Reranker-0.6B",
    "asr_model": "openai/whisper-large-v3",
}


@pytest.fixture()
def configured(monkeypatch):
    for name, value in _INTERNAL.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", ("http://192.0.2.67:30007/v1", "http://192.0.2.68:30007/v1"))
    monkeypatch.setattr(settings, "public_api_enforce_limits", False)


def _operations(document):
    for path, item in document["paths"].items():
        for method, operation in item.items():
            yield f"{method.upper()} {path}", operation


def test_the_public_document_describes_exactly_the_eleven_operations_and_the_fourteen_file_routes(configured):
    document = openapi_module.public_openapi()

    assert public_router.FILES_MOUNTED
    assert {name for name, _ in _operations(document)} == ELEVEN | FILES


def test_without_the_files_mount_the_document_describes_only_the_eleven(configured, monkeypatch):
    monkeypatch.setattr(public_router, "FILES_MOUNTED", False)
    document = openapi_module.public_openapi()

    assert {name for name, _ in _operations(document)} == ELEVEN


def test_the_router_serves_exactly_the_operations_the_document_describes(configured):
    """A route on the router that the document does not describe is public
    attack surface nobody reviewed; a documented one the router lacks is a
    promise that 404s."""
    endpoints.register(public_router.router)
    served = set()
    for route in public_router.router.routes:
        if not getattr(route, "include_in_schema", True):
            continue
        for method in route.methods or ():
            served.add(f"{method} {route.path}")

    assert served == {name for name, _ in _operations(openapi_module.public_openapi())}


def test_the_new_operations_have_stable_operation_ids_and_one_scope_each(configured):
    document = openapi_module.public_openapi()
    expected = {
        "/v1/embeddings": ("createEmbedding", "create_embedding", "embeddings.write"),
        "/v1/rerank": ("createRerank", "create_rerank", "rerank.write"),
        "/v1/audio/transcriptions": ("createTranscription", "create_transcription", "audio.write"),
    }
    endpoints.register(public_router.router)
    by_path = {route.path: route for route in public_router.router.routes}

    for path, (operation_id, operation, scope) in expected.items():
        described = document["paths"][path]["post"]
        assert described["operationId"] == operation_id
        assert by_path[path].operation_id == operation_id
        # The document names the scope the handler enforces, in the server's
        # own sentence.
        (required,) = endpoints.SCOPES[operation].required
        assert required.value == scope
        assert f"`{scope}`" in described["description"]
        assert SCOPE_DESCRIPTIONS[required] in described["description"]
        assert described["security"] == [{"bearerAuth": []}]


def test_nothing_internal_appears_anywhere_in_the_public_document(configured):
    text = json.dumps(openapi_module.public_openapi())

    for value in _INTERNAL.values():
        assert value not in text, value
    for fragment in (
        "main-engine",
        "router-engine",
        "embed-engine",
        "rerank-engine",
        "192.168.",
        "Qwen",
        "Unlimited-OCR",
        "whisper-large",
        "vllm",
        "/score",
    ):
        assert fragment not in text, fragment


def test_with_the_limits_off_the_new_operations_document_capacity_not_a_limit(configured):
    document = openapi_module.public_openapi()

    for path in ("/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions"):
        responses = document["paths"][path]["post"]["responses"]
        assert "429" not in responses, path
        assert "at capacity" in responses["503"]["description"], path
        assert "not a per-caller limit" in responses["503"]["description"], path
        assert "Retry-After" in responses["503"]["headers"], path
        # No Idempotency-Key on these three: nothing to replay, so no 409.
        assert "409" not in responses, path
        assert "RateLimit" not in responses["200"]["headers"], path


def test_with_the_limits_on_the_new_operations_advertise_the_limit_headers(configured, monkeypatch):
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    document = openapi_module.public_openapi()

    for path in ("/v1/embeddings", "/v1/rerank", "/v1/audio/transcriptions"):
        responses = document["paths"][path]["post"]["responses"]
        assert "rate_limit_error" in responses["429"]["description"], path
        assert "RateLimit" in responses["200"]["headers"], path


def test_the_transcription_operation_is_multipart_and_answers_json_or_text(configured):
    operation = openapi_module.public_openapi()["paths"]["/v1/audio/transcriptions"]["post"]

    assert list(operation["requestBody"]["content"]) == ["multipart/form-data"]
    assert set(operation["responses"]["200"]["content"]) == {"application/json", "text/plain"}


def test_the_published_ceilings_are_the_ones_the_server_enforces(configured, monkeypatch):
    monkeypatch.setattr(settings, "public_api_embed_max_inputs", 17, raising=False)
    monkeypatch.setattr(settings, "public_api_rerank_max_documents", 9, raising=False)
    schemas = openapi_module.public_openapi()["components"]["schemas"]

    embed_input = schemas["EmbeddingsRequest"]["properties"]["input"]["oneOf"][1]
    assert embed_input["maxItems"] == 17
    assert schemas["RerankRequest"]["properties"]["documents"]["maxItems"] == 9


def test_the_model_object_documents_kind_endpoints_nullable_ceilings_and_limits(configured):
    model = openapi_module.public_openapi()["components"]["schemas"]["Model"]

    for key in ("kind", "endpoints", "context_window", "default_max_output_tokens", "limits"):
        assert key in model["required"], key
    assert model["properties"]["max_output_tokens"]["type"] == ["integer", "null"]
    assert set(model["properties"]["kind"]["enum"]) == {"chat", "embedding", "rerank", "transcription"}
    response = openapi_module.public_openapi()["components"]["schemas"]["Response"]
    assert {"max_output_tokens", "incomplete_details"} <= set(response["required"])


def test_the_served_document_is_the_built_one_and_needs_no_credential(configured):
    endpoints.register(public_router.router)
    app = FastAPI()
    app.include_router(public_router.router)

    with TestClient(app) as client:
        served = client.get("/v1/openapi.json")

    assert served.status_code == 200
    assert served.json() == openapi_module.public_openapi()
