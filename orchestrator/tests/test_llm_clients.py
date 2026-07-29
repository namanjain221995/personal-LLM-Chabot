"""Offline payload-shape tests for the vLLM (OpenAI-compatible) clients.

No live services: `llm._client` is monkeypatched with a fake that records
the exact base_url and create() kwargs each call would send.

Covered per the vLLM design:
- router  → chat.completions on ROUTER_BASE_URL, temperature 0, small max_tokens
- vision  → chat.completions on VISION_BASE_URL with multimodal content
            [{"type": "text", ...}, {"type": "image_url",
              "image_url": {"url": "data:image/png;base64,<b64>"}}]
- embed   → embeddings.create on EMBED_BASE_URL with {model, input: [...]}
"""
import asyncio
from types import SimpleNamespace

from app import llm
from app.config import settings
from app.engines import router as router_engine
from app.engines import vision as vision_engine
from app.engines.vision import build_user_content, to_data_url


class FakeClient:
    """Records create() kwargs; returns a canned response or token stream."""

    def __init__(self, recorder, response=None, stream_tokens=()):
        self._recorder = recorder
        self._response = response
        self._stream_tokens = list(stream_tokens)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat_create))
        self.embeddings = SimpleNamespace(create=self._embed_create)

    async def _chat_create(self, **kwargs):
        self._recorder["chat_kwargs"] = kwargs
        if kwargs.get("stream"):
            async def gen():
                for text in self._stream_tokens:
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))]
                    )
            return gen()
        return self._response

    async def _embed_create(self, **kwargs):
        self._recorder["embed_kwargs"] = kwargs
        return self._response


def _patch_client(monkeypatch, fake, recorder):
    def fake_factory(base_url, api_key=None):
        recorder["base_url"] = base_url
        recorder["api_key"] = api_key
        return fake

    monkeypatch.setattr(llm, "_client", fake_factory)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def test_router_chat_hits_router_endpoint_with_temp0(monkeypatch):
    rec = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"route": "sql"}'))]
    )
    _patch_client(monkeypatch, FakeClient(rec, response=response), rec)

    out = asyncio.run(llm.router_chat_completion([{"role": "user", "content": "hi"}]))

    assert out == '{"route": "sql"}'
    assert rec["base_url"] == settings.router_base_url
    kwargs = rec["chat_kwargs"]
    assert kwargs["model"] == settings.router_model
    assert kwargs["temperature"] == 0.0
    assert 0 < kwargs["max_tokens"] <= 512  # small cap for a one-key JSON reply
    assert kwargs["messages"][-1] == {"role": "user", "content": "hi"}


def test_route_request_uses_router_then_parses(monkeypatch):
    rec = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"route": "report"}'))]
    )
    _patch_client(monkeypatch, FakeClient(rec, response=response), rec)

    route = asyncio.run(router_engine.route_request("build a pipeline review doc"))

    assert route == "report"
    assert rec["base_url"] == settings.router_base_url


def test_route_request_image_forces_vision_without_model_call(monkeypatch):
    rec = {}
    _patch_client(monkeypatch, FakeClient(rec), rec)
    assert asyncio.run(router_engine.route_request("what is this?", has_image=True)) == "vision"
    assert "base_url" not in rec  # no client was ever built


# ---------------------------------------------------------------------------
# Vision (multimodal content with data: URL)
# ---------------------------------------------------------------------------

def test_to_data_url_wraps_raw_base64():
    assert to_data_url("aGVsbG8=") == "data:image/png;base64,aGVsbG8="


def test_to_data_url_preserves_existing_data_url():
    url = "data:image/jpeg;base64,aGVsbG8="
    assert to_data_url(url) == url


def test_build_user_content_shape():
    content = build_user_content("What is the total due?", "aGVsbG8=")
    assert content == [
        {"type": "text", "text": "What is the total due?"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aGVsbG8="},
        },
    ]


def test_vision_engine_sends_multimodal_payload_and_contract_meta(monkeypatch):
    rec = {}
    _patch_client(monkeypatch, FakeClient(rec, stream_tokens=["The total ", "is $42."]), rec)

    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(
        vision_engine.run_vision_engine("total?", "aGVsbG8=", [], emit)
    )

    assert answer == "The total is $42."
    assert rec["base_url"] == settings.vision_base_url
    kwargs = rec["chat_kwargs"]
    assert kwargs["model"] == settings.vision_model
    assert kwargs["stream"] is True
    user_msg = kwargs["messages"][-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == [
        {"type": "text", "text": "total?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    # §10: tokens stream first, then exactly ONE meta with the contract shape.
    assert [d["text"] for e, d in events if e == "token"] == ["The total ", "is $42."]
    assert [d for e, d in events if e == "meta"] == [{"route": "vision"}]


# ---------------------------------------------------------------------------
# Embeddings ({model, input})
# ---------------------------------------------------------------------------

def test_embed_texts_payload_shape_and_input_order(monkeypatch):
    rec = {}
    response = SimpleNamespace(
        data=[  # deliberately out of order: results must be re-sorted by index
            SimpleNamespace(index=1, embedding=[0.2, 0.2]),
            SimpleNamespace(index=0, embedding=[0.1, 0.1]),
        ]
    )
    _patch_client(monkeypatch, FakeClient(rec, response=response), rec)

    vectors = asyncio.run(llm.embed_texts(["alpha", "beta"]))

    assert rec["base_url"] == settings.embed_base_url
    assert rec["embed_kwargs"] == {
        "model": settings.embed_model,
        "input": ["alpha", "beta"],
    }
    assert vectors == [[0.1, 0.1], [0.2, 0.2]]
