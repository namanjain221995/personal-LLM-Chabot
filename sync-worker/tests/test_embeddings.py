"""Offline tests for the OpenAI-compatible embedding client (no live vLLM).

HTTP is mocked with httpx.MockTransport; requests never leave the process.
"""

import json

import httpx
import pytest

from syncworker.rag_index import EMBED_BATCH_SIZE, OpenAIEmbedder

BASE_URL = "http://vllm-embed:30003/v1"
MODEL = "Qwen/Qwen3-Embedding-0.6B"


def _openai_embeddings_response(inputs: list[str]) -> dict:
    """Build an OpenAI-shape /embeddings response: .data[i].embedding, in order."""
    return {
        "object": "list",
        "model": MODEL,
        "data": [
            {"object": "embedding", "index": i, "embedding": [float(i), float(i) + 0.5]}
            for i in range(len(inputs))
        ],
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def _make_embedder(
    handler, base_url: str = BASE_URL, *, api_key: str = ""
) -> OpenAIEmbedder:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAIEmbedder(base_url, MODEL, http=client, api_key=api_key)


def test_posts_openai_payload_to_embeddings_endpoint():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json=_openai_embeddings_response(body["input"]))

    embedder = _make_embedder(handler)
    vectors = embedder.embed(["alpha", "beta"])

    assert len(seen) == 1
    assert str(seen[0].url) == f"{BASE_URL}/embeddings"
    body = json.loads(seen[0].content)
    assert body == {"model": MODEL, "input": ["alpha", "beta"]}
    assert vectors == [[0.0, 0.5], [1.0, 1.5]]


def test_vectors_come_back_in_input_order():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = _openai_embeddings_response(body["input"])
        payload["data"].reverse()  # servers may return results out of order
        return httpx.Response(200, json=payload)

    embedder = _make_embedder(handler)
    texts = [f"text-{i}" for i in range(5)]
    vectors = embedder.embed(texts)
    # data[i].embedding maps 1:1, in order, onto input i
    assert vectors == [[float(i), float(i) + 0.5] for i in range(5)]


def test_optional_api_key_is_sent_as_bearer_without_changing_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        seen["body"] = body
        return httpx.Response(200, json=_openai_embeddings_response(body["input"]))

    _make_embedder(handler, api_key="ephemeral-local-key").embed(["alpha"])

    assert seen == {
        "authorization": "Bearer ephemeral-local-key",
        "body": {"model": MODEL, "input": ["alpha"]},
    }


def test_empty_api_key_sends_no_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        body = json.loads(request.content)
        return httpx.Response(200, json=_openai_embeddings_response(body["input"]))

    _make_embedder(handler).embed(["alpha"])
    assert seen["authorization"] is None


def test_batches_of_32_and_concatenates_in_order():
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        batch_sizes.append(len(body["input"]))
        return httpx.Response(200, json=_openai_embeddings_response(body["input"]))

    embedder = _make_embedder(handler)
    texts = [f"t{i}" for i in range(EMBED_BATCH_SIZE * 2 + 6)]  # 70
    vectors = embedder.embed(texts)

    assert batch_sizes == [EMBED_BATCH_SIZE, EMBED_BATCH_SIZE, 6]
    assert len(vectors) == len(texts)
    # last batch's first vector is index 0 of its own response
    assert vectors[EMBED_BATCH_SIZE * 2] == [0.0, 0.5]


def test_trailing_slash_in_base_url_is_normalized():
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        body = json.loads(request.content)
        return httpx.Response(200, json=_openai_embeddings_response(body["input"]))

    embedder = _make_embedder(handler, base_url=BASE_URL + "/")
    embedder.embed(["x"])
    assert urls == [f"{BASE_URL}/embeddings"]


def test_vector_count_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payload = _openai_embeddings_response(body["input"])
        payload["data"] = payload["data"][:-1]  # drop one embedding
        return httpx.Response(200, json=payload)

    embedder = _make_embedder(handler)
    with pytest.raises(RuntimeError, match="vectors"):
        embedder.embed(["a", "b", "c"])


def test_http_error_raises_for_caller_fail_soft():
    """The embedder raises; main.py catches it so RAG never blocks the sync."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "loading model"})

    embedder = _make_embedder(handler)
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed(["a"])
