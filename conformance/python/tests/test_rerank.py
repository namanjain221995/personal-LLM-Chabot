"""POST /v1/rerank with raw httpx — the OpenAI SDK has no rerank method
(CONTRACT-3 §8.5, Cohere/Jina shape). Planned 2026-09-13."""
from __future__ import annotations

import re
import uuid

import pytest

from techsara_conformance import asserts

pytestmark = pytest.mark.feature("rerank")

QUERY = "How do I rotate an API key?"
DOCUMENTS = [
    "Bananas are an excellent source of potassium.",
    {"text": "To rotate an API key, open the console, choose the key and press Rotate; the old key keeps working during the overlap."},
    "Webhooks are signed with HMAC-SHA256.",
]


def test_rerank_orders_documents_by_relevance_cut_to_top_n_with_documents(raw, target):
    response = raw.post(
        "rerank",
        json={"model": target.models["rerank"], "query": QUERY, "documents": DOCUMENTS, "top_n": 2, "return_documents": True},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.headers.get("x-request-id")
    body = response.json()
    assert body["object"] == "rerank" and body["model"] == target.models["rerank"]
    # CONTRACT-3 §8.5 / §16: the id is the ledger generation id, `rrk_<24 hex>`.
    # The first version expected `rerank_` and would have failed a correct
    # server (adversarial review with a contract-shaped mock, 2026-09-13).
    assert re.fullmatch(r"rrk_[0-9a-f]{24}", body["id"]), body["id"]
    results = body["results"]
    assert len(results) == 2, "cut to top_n"
    scores = [r["relevance_score"] for r in results]
    assert scores == sorted(scores, reverse=True), scores
    assert all(0.0 <= s <= 1.0 for s in scores), scores
    assert results[0]["index"] == 1, f"the key-rotation passage should rank first: {results}"
    assert results[0]["document"] == {"text": DOCUMENTS[1]["text"]}
    assert body["usage"] is None or body["usage"]["input_tokens"] > 0


def test_without_top_n_or_return_documents_every_document_is_scored_and_none_echoed(raw, target):
    response = raw.post("rerank", json={"model": target.models["rerank"], "query": QUERY, "documents": DOCUMENTS})
    assert response.status_code == 200, response.text[:300]
    results = response.json()["results"]
    assert sorted(r["index"] for r in results) == [0, 1, 2]
    assert all("document" not in r for r in results)


@pytest.mark.parametrize(
    "body_patch",
    [{"documents": []}, {"query": ""}, {"top_n": 0}, {"frobnicate": 1}],
    ids=["no-documents", "empty-query", "top_n-zero", "unknown-field"],
)
def test_invalid_rerank_bodies_are_400(raw, target, body_patch):
    body = {"model": target.models["rerank"], "query": QUERY, "documents": DOCUMENTS, **body_patch}
    response = raw.post("rerank", json=body)
    asserts.envelope(response.status_code, response.json(), response.headers, code="invalid_request_error")


def test_an_idempotency_key_on_rerank_is_refused_with_400(raw, target):
    response = raw.post(
        "rerank",
        json={"model": target.models["rerank"], "query": QUERY, "documents": DOCUMENTS},
        headers={"Idempotency-Key": f"conformance-{uuid.uuid4()}"},
    )
    asserts.envelope(response.status_code, response.json(), response.headers, code="invalid_request_error", param="Idempotency-Key")


@pytest.mark.limited_key
def test_rerank_without_the_scope_is_403(target):
    if not target.limited_api_key:
        pytest.skip("no limited key: set TECHSARA_LIMITED_API_KEY")
    import httpx

    response = httpx.post(
        f"{target.base_url}/rerank",
        json={"model": target.models["rerank"], "query": QUERY, "documents": DOCUMENTS},
        headers={"Authorization": f"Bearer {target.limited_api_key}"},
        timeout=30.0,
    )
    asserts.envelope(response.status_code, response.json(), response.headers, code="insufficient_scope")
