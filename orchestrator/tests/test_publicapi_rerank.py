"""`POST /v1/rerank` over real HTTP, real keys and a real database.

The reranker is an `httpx.MockTransport` answering vLLM's `/score` shape
(tests/publicapi_sidecar_support.py); the template, the packing, the gate,
the ledger and the envelope are the production code.
"""
from __future__ import annotations

import httpx

from app import rerank
from app.config import settings
from app.model_capabilities import RerankerBackend
from app.publicapi import capacity
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    RERANK_URL,
    _pepper,
    api,
    assert_nothing_internal,
    auth,
    daily,
    engines_configured,
    install_transport,
    platform,
    usage_events,
)

URL = "/v1/rerank"


def _body(**overrides):
    body = {
        "model": "techsara-rerank",
        "query": "At what temperature does water boil at sea level?",
        "documents": ["The museum opens at nine.", {"text": "Water boils at 100 degrees Celsius."}, "Boiling."],
    }
    body.update(overrides)
    return body


def _scores(values):
    """A `/score` engine whose score for a document is looked up by its text."""

    def handler(request, body):
        import json

        payload = json.loads(body)
        data = []
        for index, document in enumerate(payload["text_2"]):
            score = 0.5
            for needle, value in values.items():
                if needle in document:
                    score = value
            data.append({"index": index, "object": "score", "score": score})
        tokens = 10 * len(payload["text_2"])
        return httpx.Response(
            200,
            json={"object": "list", "data": data, "usage": {"prompt_tokens": tokens, "total_tokens": tokens}},
        )

    return handler


# ------------------------------------------------------------- the answer --


def test_results_come_back_highest_score_first_cut_to_top_n_with_documents_on_request(api):
    install_transport(_scores({"museum": 0.01, "100 degrees": 0.99, "Boiling": 0.4}))

    response = api.post(URL, json=_body(top_n=2, return_documents=True), headers=auth())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["object"] == "rerank" and payload["model"] == "techsara-rerank"
    assert payload["id"].startswith("rrk_")
    assert payload["results"] == [
        {"index": 1, "relevance_score": 0.99, "document": {"text": "Water boils at 100 degrees Celsius."}},
        {"index": 2, "relevance_score": 0.4, "document": {"text": "Boiling."}},
    ]
    assert payload["usage"] == {"input_tokens": 30, "total_tokens": 30}
    assert_nothing_internal(response)


def test_without_return_documents_the_results_carry_no_text(api):
    install_transport(_scores({"100 degrees": 0.9}))

    results = api.post(URL, json=_body(), headers=auth()).json()["results"]

    assert len(results) == 3
    assert all(set(result) == {"index", "relevance_score"} for result in results)


def test_the_model_card_template_is_applied_and_nothing_is_truncated(api):
    """`rerank.format_query` cuts the query at 600 characters and a document at
    3,000; the public surface must score what the caller sent."""
    recorded = install_transport(_scores({}))
    query = "why " + "q" * 900
    document = "d" * 5000

    response = api.post(
        URL,
        json=_body(query=query, documents=[document], instruction="Find the passage that answers."),
        headers=auth(),
    )

    assert response.status_code == 200
    sent = recorded.json_bodies()[0]
    assert str(recorded.requests[0].url) == f"{RERANK_URL}/score"
    assert sent["model"] == settings.rerank_model
    assert sent["text_1"].startswith(rerank.PREFIX)
    assert "<Instruct>: Find the passage that answers.\n" in sent["text_1"]
    assert sent["text_1"].endswith(f"<Query>: {query}\n")
    assert sent["text_2"] == [f"<Document>: {document}{rerank.SUFFIX}"]


def test_equal_scores_are_returned_as_they_are_and_never_refused_as_degenerate(api):
    install_transport(_scores({}))

    response = api.post(URL, json=_body(documents=[f"doc {i}" for i in range(8)]), headers=auth())

    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["index"] for result in results] == list(range(8))
    assert {result["relevance_score"] for result in results} == {0.5}


def test_documents_are_packed_into_score_calls_of_sixteen_under_the_rerank_gate(api, monkeypatch):
    recorded = install_transport(_scores({}))
    gates = []
    real_hold = capacity.hold

    def recording_hold(gate, **kwargs):
        gates.append(gate)
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", recording_hold)

    response = api.post(URL, json=_body(documents=[f"doc {i}" for i in range(20)]), headers=auth())

    assert response.status_code == 200
    assert [len(body["text_2"]) for body in recorded.json_bodies()] == [16, 4]
    assert gates == ["rerank", "rerank"]
    assert response.json()["usage"]["input_tokens"] == 200


# ------------------------------------------------------ who may call it --


def test_a_request_without_a_key_is_a_401_and_never_reaches_the_engine(api):
    recorded = install_transport(_scores({}))

    response = api.post(URL, json=_body())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert recorded.calls == 0


def test_a_key_without_rerank_write_is_a_403_naming_the_scope(api):
    recorded = install_transport(_scores({}))

    response = api.post(URL, json=_body(), headers=auth("narrow"))

    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="rerank.write"'
    assert recorded.calls == 0


def test_an_origin_outside_the_projects_allowlist_is_refused_before_any_work(api, platform):
    from app.apiplatform import projects
    from tests.publicapi_sidecar_support import WORKSPACE

    projects.update_project(
        platform["project"]["id"], WORKSPACE, allowed_origins=["https://app.customer.example"]
    )
    recorded = install_transport(_scores({}))

    refused = api.post(URL, json=_body(), headers={**auth(), "Origin": "https://evil.example"})

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "origin_not_allowed"
    assert "access-control-allow-credentials" not in {k.lower() for k in refused.headers}
    assert recorded.calls == 0


def test_a_deployment_without_a_remote_reranker_does_not_offer_the_model(api, monkeypatch):
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.INPROCESS)
    recorded = install_transport(_scores({}))

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 404
    assert recorded.calls == 0


def test_the_embedding_model_on_the_rerank_endpoint_is_a_400_naming_model(api):
    recorded = install_transport(_scores({}))

    response = api.post(URL, json=_body(model="techsara-embed"), headers=auth())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"
    assert recorded.calls == 0


# ------------------------------------------------------------ the body --


def test_a_body_over_the_cap_is_a_413_before_the_engine(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_body_bytes", 256, raising=False)
    recorded = install_transport(_scores({}))

    response = api.post(URL, json=_body(documents=["x" * 1000]), headers=auth())

    assert response.status_code == 413
    assert recorded.calls == 0


def test_a_top_n_that_is_not_a_positive_integer_and_a_blank_document_are_refused(api):
    recorded = install_transport(_scores({}))

    boolean = api.post(URL, json=_body(top_n=True), headers=auth())
    zero = api.post(URL, json=_body(top_n=0), headers=auth())
    blank = api.post(URL, json=_body(documents=["fine", {"text": "  "}]), headers=auth())
    extra = api.post(URL, json=_body(rank_fields=["title"]), headers=auth())

    assert [r.status_code for r in (boolean, zero, blank, extra)] == [400, 400, 400, 400]
    assert blank.json()["error"]["param"] == "documents.1"
    assert extra.json()["error"]["param"] == "rank_fields"
    assert recorded.calls == 0


# ---------------------------------------------------------- the engine --


def test_an_unreachable_reranker_is_a_503_with_retry_after_and_counts_as_an_error(api, platform):
    def down(request, body):
        raise httpx.ConnectError("connection refused")

    install_transport(down)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert_nothing_internal(response)
    assert daily(platform["project"]["id"])["errors"] == 1


def test_a_reranker_that_answers_500_is_a_503_not_a_500(api):
    install_transport(lambda request, body: httpx.Response(500, text="Traceback: CUDA error at /app/x.py"))

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert "CUDA" not in response.text and "/app/" not in response.text


def test_a_pair_over_the_window_is_named_by_document_index(api):
    def engine(request, body):
        import json

        payload = json.loads(body)
        if any("enormous" in document for document in payload["text_2"]):
            return httpx.Response(400, json={"message": "maximum context length is 4096 tokens"})
        return _scores({})(request, body)

    recorded = install_transport(engine)

    response = api.post(URL, json=_body(documents=["a", "b", "enormous"]), headers=auth())

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("context_length_exceeded", "documents.2")
    assert [len(body["text_2"]) for body in recorded.json_bodies()] == [3, 1, 1, 1]


def test_a_malformed_score_reply_is_a_503_not_a_misattributed_score(api):
    install_transport(
        lambda request, body: httpx.Response(200, json={"data": [{"index": 0, "score": 0.9}]})
    )

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503


# ------------------------------------------------------------ metering --


def test_a_completed_rerank_is_one_usage_row_with_the_engine_count(api, platform):
    install_transport(_scores({}))

    response = api.post(URL, json=_body(top_n=1), headers=auth())

    assert response.status_code == 200
    rows = usage_events("v1_rerank")
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["input_tokens"], rows[0]["output_tokens"]) == ("ok", 30, 0)
    assert rows[0]["meta"]["documents"] == 3 and rows[0]["meta"]["top_n"] == 1
    assert rows[0]["generation_id"] == response.json()["id"]
    assert daily(platform["project"]["id"]) == {
        "requests": 1,
        "input_tokens": 30,
        "output_tokens": 0,
        "errors": 0,
    }
