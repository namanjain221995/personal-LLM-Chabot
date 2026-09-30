"""Focused offline tests for model/runtime capability routing."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from app import health, llm
from app.config import Settings, settings
from app.engines import rag
from app.model_capabilities import ReasoningField, RerankerBackend


class _StreamClient:
    def __init__(self, recorder, deltas):
        self.recorder = recorder
        self.deltas = deltas
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.recorder.update(kwargs)

        async def generate():
            for delta in self.deltas:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

        return generate()


def test_capability_env_disables_unsupported_reasoning_fields(monkeypatch):
    monkeypatch.setenv("MAIN_PROVIDER", "mlx-community")
    monkeypatch.setenv("MAIN_BACKEND", "mlx-lm")
    monkeypatch.setenv("MAIN_SUPPORTS_REASONING", "false")
    monkeypatch.setenv("MAIN_REASONING_FIELD", "reasoning_content")
    monkeypatch.setenv("MAIN_EXTRA_BODY_ALLOWED", "")
    monkeypatch.setenv("MAIN_CONTEXT_LENGTH", "16384")

    capabilities = Settings().main_capabilities

    assert capabilities.provider == "mlx-community"
    assert capabilities.backend == "mlx-lm"
    assert capabilities.supports_reasoning is False
    assert capabilities.reasoning_field is ReasoningField.NONE
    assert capabilities.extra_body_arguments == frozenset()
    assert capabilities.context_length == 16384


def test_reranker_backend_defaults_and_legacy_disable(monkeypatch):
    monkeypatch.delenv("RERANK_BACKEND", raising=False)
    monkeypatch.delenv("RERANK_ENABLED", raising=False)
    assert Settings().rerank_backend is RerankerBackend.INPROCESS

    monkeypatch.setenv("RERANK_ENABLED", "false")
    monkeypatch.setenv("RERANK_BACKEND", "remote")
    configured = Settings()
    assert configured.rerank_backend is RerankerBackend.DISABLED
    assert configured.rerank_enabled is False
    assert configured.reranker_capabilities.enabled is False


def test_stream_omits_reasoning_extension_when_unsupported(monkeypatch):
    recorder = {}
    client = _StreamClient(recorder, [SimpleNamespace(content="answer")])
    monkeypatch.setattr(llm, "_client", lambda *args, **kwargs: client)

    async def fit(messages, **kwargs):
        return list(messages), 100

    monkeypatch.setattr(llm.context, "fit_request", fit)
    monkeypatch.setattr(
        settings,
        "main_capabilities",
        replace(
            settings.main_capabilities,
            supports_reasoning=False,
            reasoning_field=ReasoningField.NONE,
            extra_body_arguments=frozenset(),
        ),
    )

    async def collect():
        return [
            item
            async for item in llm.stream_chat_events(
                [{"role": "user", "content": "hello"}], effort="high"
            )
        ]

    assert asyncio.run(collect()) == [("token", "answer")]
    assert "extra_body" not in recorder


def test_stream_missing_reasoning_field_keeps_token_sse(monkeypatch):
    recorder = {}
    # No reasoning/reasoning_content/model_extra attributes: this is a normal
    # OpenAI-compatible content delta, not an exceptional response.
    client = _StreamClient(recorder, [SimpleNamespace(content="still here")])
    monkeypatch.setattr(llm, "_client", lambda *args, **kwargs: client)

    async def fit(messages, **kwargs):
        return list(messages), 100

    monkeypatch.setattr(llm.context, "fit_request", fit)

    async def collect():
        return [
            item
            async for item in llm.stream_chat_events(
                [{"role": "user", "content": "hello"}], effort="medium"
            )
        ]

    assert asyncio.run(collect()) == [("token", "still here")]
    assert recorder["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True}
    }


class _ScoreResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": [
                {"index": 2, "score": 0.2},
                {"index": 0, "score": 0.1},
                {"index": 1, "score": 0.9},
            ]
        }


class _ScoreClient:
    recorder = None

    def __init__(self, **kwargs):
        self.recorder["client"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.recorder["url"] = url
        self.recorder["request"] = kwargs
        return _ScoreResponse()


def test_remote_reranker_uses_score_api_and_response_indices(monkeypatch):
    """The RAG engine scores through the ONE shared client (app/rerank.py):
    the model's prompt template around a CRM-specific instruction, the
    root /score endpoint, strict index handling."""
    from app import rerank

    recorder = {}
    _ScoreClient.recorder = recorder
    monkeypatch.setattr(rerank.httpx, "AsyncClient", _ScoreClient)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "rerank_base_url", "http://reranker:9000/v1/")
    monkeypatch.setattr(settings, "rerank_api_key", "")
    monkeypatch.setattr(settings, "rerank_model", "test-reranker")
    monkeypatch.setattr(settings, "rerank_canary_enabled", False)

    class _Caps:
        enabled = True
        supports_reranking = True
        requires_authentication = False

    monkeypatch.setattr(settings, "reranker_capabilities", _Caps())
    rerank.reset_for_tests()
    hits = [{"text": "first"}, {"text": "second"}, {"text": "third"}]

    ranked = asyncio.run(rag._remote_rerank("query", hits, 2))

    assert ranked == [hits[1], hits[2]]
    assert recorder["url"] == "http://reranker:9000/score"
    body = recorder["request"]["json"]
    assert body["model"] == "test-reranker"
    assert body["text_1"].startswith(rerank.PREFIX)
    assert rag._RECORD_INSTRUCTION in body["text_1"]
    assert body["text_1"].endswith("<Query>: query\n")
    assert [t.startswith("<Document>: ") and t.endswith(rerank.SUFFIX) for t in body["text_2"]] == [True] * 3
    assert [t[len("<Document>: "):].split("<|im_end|>")[0] for t in body["text_2"]] == ["first", "second", "third"]


def test_remote_reranker_failure_preserves_vector_order(monkeypatch):
    hits = [{"text": "first"}, {"text": "second"}, {"text": "third"}]

    async def retrieve(*args, **kwargs):
        return hits

    async def unavailable(*args, **kwargs):
        raise RuntimeError("score endpoint is cold")

    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "_remote_rerank", unavailable)
    monkeypatch.setattr(
        rag,
        "_rerank",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("remote mode must not enter the torch reranker")
        ),
    )
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(
        settings,
        "reranker_capabilities",
        replace(
            settings.reranker_capabilities,
            backend="remote",
            enabled=True,
            supports_reranking=True,
        ),
    )
    monkeypatch.setattr(settings, "rag_final_k", 2)

    assert asyncio.run(rag.select_context("query")) == hits[:2]


def test_disabled_reranker_never_initializes_any_backend(monkeypatch):
    hits = [{"text": "first"}, {"text": "second"}]

    async def retrieve(*args, **kwargs):
        return hits

    def forbidden(*args, **kwargs):
        raise AssertionError("a disabled reranker must not initialize a backend")

    monkeypatch.setattr(rag, "retrieve", retrieve)
    monkeypatch.setattr(rag, "_rerank", forbidden)
    monkeypatch.setattr(rag, "_remote_rerank", forbidden)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.DISABLED)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(
        settings,
        "reranker_capabilities",
        replace(
            settings.reranker_capabilities,
            backend="disabled",
            enabled=False,
            supports_reranking=False,
        ),
    )

    assert asyncio.run(rag.select_context("query")) == hits


def test_health_adds_optional_capabilities_without_changing_required_checks(monkeypatch):
    async def endpoint_ok(client, base_url):
        return {"status": "ok"}

    async def ocr_degraded(client):
        return {"status": "degraded", "detail": "OCR is cold"}

    async def reranker_disabled(client):
        return {"status": "disabled", "detail": "disabled by configuration"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(health, "_probe_ocr", ocr_degraded)
    monkeypatch.setattr(health, "_probe_reranker", reranker_disabled)
    monkeypatch.setattr(health, "_check_duckdb", lambda path: {"status": "ok"})
    monkeypatch.setattr(health, "_check_app_db", lambda: {"status": "ok"})
    monkeypatch.setattr(
        health,
        "_check_embedding_index",
        lambda: {"status": "empty", "detail": "not indexed yet"},
    )

    result = asyncio.run(health.check_dependencies())

    assert result["status"] == "ok"
    assert set(result["checks"]) == {
        "vllm",
        "vllm-router",
        "vllm-embed",
        "duckdb",
        "app_db",
    }
    assert result["capability_status"] == "degraded"
    assert result["capabilities"]["ocr"]["status"] == "degraded"
    assert result["capabilities"]["reranker"]["status"] == "disabled"


def test_health_marks_embedding_capability_degraded_on_index_mismatch(monkeypatch):
    async def endpoint_ok(client, base_url):
        return {"status": "ok"}

    async def optional_disabled(client):
        return {"status": "disabled", "detail": "disabled by configuration"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(health, "_probe_ocr", optional_disabled)
    monkeypatch.setattr(health, "_probe_reranker", optional_disabled)
    monkeypatch.setattr(health, "_check_duckdb", lambda path: {"status": "ok"})
    monkeypatch.setattr(health, "_check_app_db", lambda: {"status": "ok"})
    monkeypatch.setattr(
        health,
        "_check_embedding_index",
        lambda: {
            "status": "error",
            "detail": "configured embedding model differs; use a new empty index",
        },
    )

    result = asyncio.run(health.check_dependencies())

    # Existing readiness contract stays required-service-only, while the
    # additive capability status exposes the unsafe vector space.
    assert result["status"] == "ok"
    assert result["checks"]["vllm-embed"] == {"status": "ok"}
    assert result["capability_status"] == "degraded"
    assert result["capabilities"]["embed"]["status"] == "degraded"
    assert result["capabilities"]["embed"]["index"]["status"] == "error"


def test_enabled_ocr_health_uses_its_configured_endpoint(monkeypatch):
    called = {}

    async def endpoint_ok(client, base_url):
        called["base_url"] = base_url
        return {"status": "ok"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(
        settings,
        "ocr_capabilities",
        replace(settings.ocr_capabilities, enabled=True, supports_ocr=True),
    )

    result = asyncio.run(health._probe_ocr(object()))

    assert result == {"status": "ok"}
    assert called["base_url"] == settings.ocr_base_url


def test_enabled_remote_reranker_health_runs_score_probe(monkeypatch):
    called = {}

    async def score_ok(client):
        called["client"] = client
        return {"status": "ok"}

    sentinel = object()
    monkeypatch.setattr(health, "_probe_remote_reranker", score_ok)
    monkeypatch.setattr(settings, "rerank_backend", RerankerBackend.REMOTE)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(
        settings,
        "reranker_capabilities",
        replace(
            settings.reranker_capabilities,
            backend="remote",
            enabled=True,
            supports_reranking=True,
        ),
    )

    result = asyncio.run(health._probe_reranker(sentinel))

    assert result == {"status": "ok"}
    assert called["client"] is sentinel


# ---------------------------------------------------------------------------
# engines.rag.retrieve — hit projection and the ANN switches (ADR-0001 D8),
# against a small temporary LanceDB table shaped like the sync-worker's.
# ---------------------------------------------------------------------------

_SF_DIM = 8
_NEEDLE_ID = "006000000000001AAA"


def _seed_sf_chunks(tmp_path, needle, *, with_index: bool):
    import json
    import random

    import lancedb
    import pyarrow as pa

    from app.embedding_index import METADATA_FILENAME

    directory = tmp_path / "lancedb"
    directory.mkdir()
    (directory / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "table": "chunks",
                "model_id": settings.embed_model,
                "dimension": _SF_DIM,
            }
        )
    )
    schema = pa.schema(
        [
            pa.field("vector", pa.list_(pa.float32(), _SF_DIM)),
            pa.field("text", pa.string()),
            pa.field("object", pa.string()),
            pa.field("record_id", pa.string()),
            pa.field("field", pa.string()),
            pa.field("system_modstamp", pa.string()),
        ]
    )
    table = lancedb.connect(str(directory)).create_table("chunks", schema=schema)
    rng = random.Random(7)
    rows = [
        {
            "vector": [rng.random() for _ in range(_SF_DIM)],
            "text": f"note {i}",
            "object": "Account",
            "record_id": f"001{i:015d}",
            "field": "Description",
            "system_modstamp": "",
        }
        for i in range(60)
    ]
    rows.append(
        {
            "vector": list(needle),
            "text": "the needle",
            "object": "Opportunity",
            "record_id": _NEEDLE_ID,
            "field": "Description",
            "system_modstamp": "",
        }
    )
    table.add(rows)
    if with_index:
        from lancedb.index import IvfFlat

        table.create_index(
            "vector", config=IvfFlat(distance_type="l2", num_partitions=4), replace=True
        )
    return directory


def _point_retrieve_at(monkeypatch, directory, needle):
    monkeypatch.setattr(settings, "lancedb_dir", str(directory))
    monkeypatch.setattr(settings, "lancedb_table", "chunks")
    monkeypatch.setattr(settings, "knowledge_ann_bypass", False)
    monkeypatch.setattr(settings, "rag_top_k", 5)

    async def embed_texts(texts, **kwargs):
        return [list(needle) for _ in texts]

    monkeypatch.setattr(rag.llm, "embed_texts", embed_texts)


def _spy_query_builder(monkeypatch):
    """Record which ANN knobs retrieve() turns on the query it builds."""
    from lancedb.query import LanceVectorQueryBuilder

    calls = []
    for name in ("nprobes", "refine_factor", "bypass_vector_index"):
        original = getattr(LanceVectorQueryBuilder, name)

        def wrapped(self, *args, _name=name, _original=original):
            calls.append((_name, *args))
            return _original(self, *args)

        monkeypatch.setattr(LanceVectorQueryBuilder, name, wrapped)
    return calls


def test_retrieve_ships_prompt_columns_only_and_flat_scans_an_unindexed_table(
    tmp_path, monkeypatch
):
    needle = [0.5] * _SF_DIM
    directory = _seed_sf_chunks(tmp_path, needle, with_index=False)
    _point_retrieve_at(monkeypatch, directory, needle)
    calls = _spy_query_builder(monkeypatch)

    hits = asyncio.run(rag.retrieve("which opportunity is the needle"))

    assert len(hits) == 5
    assert hits[0]["record_id"] == _NEEDLE_ID
    assert hits[0]["object"] == "Opportunity"
    assert hits[0]["text"] == "the needle"
    # The 1024-float vector no longer rides along; distance still does.
    assert set(hits[0]) == {"record_id", "object", "text", "_distance"}
    assert [h["_distance"] for h in hits] == sorted(h["_distance"] for h in hits)
    # No index: no probes to set, nothing to bypass.
    assert calls == []


def test_retrieve_probes_the_ann_index_when_the_worker_built_one(tmp_path, monkeypatch):
    needle = [0.25] * _SF_DIM
    directory = _seed_sf_chunks(tmp_path, needle, with_index=True)
    _point_retrieve_at(monkeypatch, directory, needle)
    monkeypatch.setattr(settings, "web_index_nprobes", 7)
    calls = _spy_query_builder(monkeypatch)

    hits = asyncio.run(rag.retrieve("which opportunity is the needle"))

    assert hits[0]["record_id"] == _NEEDLE_ID
    assert "vector" not in hits[0]
    assert calls == [("nprobes", 7), ("refine_factor", 2)]


def test_retrieve_bypass_flag_forces_the_flat_scan_without_touching_data(
    tmp_path, monkeypatch
):
    needle = [0.75] * _SF_DIM
    directory = _seed_sf_chunks(tmp_path, needle, with_index=True)
    _point_retrieve_at(monkeypatch, directory, needle)
    monkeypatch.setattr(settings, "knowledge_ann_bypass", True)
    calls = _spy_query_builder(monkeypatch)

    hits = asyncio.run(rag.retrieve("which opportunity is the needle"))

    assert hits[0]["record_id"] == _NEEDLE_ID
    assert calls == [("bypass_vector_index",)]
    # The rollback is reader-side only: the index the worker built is intact.
    import lancedb

    table = lancedb.connect(str(directory)).open_table("chunks")
    assert [i.name for i in table.list_indices()] == ["vector_idx"]
