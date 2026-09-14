"""techsara-8b-vision and techsara-ocr on `/v1` — the sidecar chat path.

The router and the OCR engine are reached through `publicapi/engines.
stream_chat`, never through `llm.stream_chat_events` (which refuses a sidecar
URL) and never with anything a caller sent as the address. What is stubbed is
ONLY the OpenAI client `llm._client` hands back — the request the engine would
receive is recorded exactly, so the assertions are about what leaves this
process: the served model name, the max_tokens the planner decided, the "OCR"
prompt, the reasoning switch, and that nothing internal comes back out.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, Dict, List, Optional

import httpx
import openai
import pytest

from app import db, llm
from app.config import settings
from app.publicapi import engines, errors, events, registry, streaming
from tests.test_publicapi_routes import TOKENS, _auth, _pepper, api, platform  # noqa: F401
from tests.publicapi_fake_engine import set_setting

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40).decode()
DATA_URL = f"data:image/png;base64,{PNG}"


class _Obj:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _chunk(content: Optional[str] = None, finish: Optional[str] = None, usage=None):
    choices = [] if content is None and finish is None else [
        _Obj(delta=_Obj(content=content, model_extra={}), finish_reason=finish)
    ]
    return _Obj(choices=choices, usage=usage)


class FakeSidecar:
    """What `llm._client(base_url, …)` returns, recording every call."""

    def __init__(
        self,
        pieces=("Hello", " there"),
        *,
        finish: str = "stop",
        usage=(21, 2),
        open_error: Optional[BaseException] = None,
        delay: float = 0.0,
    ) -> None:
        self.pieces = list(pieces)
        self.finish = finish
        self.usage = usage
        self.open_error = open_error
        self.delay = delay
        self.requests: List[Dict[str, Any]] = []
        self.clients: List[tuple] = []
        self.closed = False
        self.chat = self
        self.completions = self

    def client(self, base_url, api_key=None, *, read_timeout=None):
        self.clients.append((base_url, read_timeout))
        return self

    async def create(self, **request):
        self.requests.append(request)
        if self.open_error is not None:
            raise self.open_error
        return self._stream()

    async def _stream(self):
        try:
            for piece in self.pieces:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield _chunk(piece)
            yield _chunk(finish=self.finish)
            if self.usage is not None:
                yield _chunk(usage=_Obj(prompt_tokens=self.usage[0], completion_tokens=self.usage[1]))
        finally:
            self.closed = True


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No probe of a real engine's /models from a test run."""

    async def no_probe(engine):
        return None

    monkeypatch.setattr(engines, "served_window", no_probe)
    monkeypatch.setattr(settings, "ocr_enabled", True)


@pytest.fixture()
def sidecar(monkeypatch):
    def install(fake: FakeSidecar) -> FakeSidecar:
        monkeypatch.setattr(llm, "_client", fake.client)
        return fake

    return install


@pytest.fixture()
def main_engine(monkeypatch):
    calls = []

    def fake(messages, **kwargs):
        calls.append(kwargs)

        async def run():
            yield ("token", "main")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", fake)
    return calls


def _image_input(text: Optional[str] = None, images: int = 1):
    content: List[Dict[str, Any]] = []
    if text is not None:
        content.append({"type": "input_text", "text": text})
    content.extend({"type": "input_image", "image_url": DATA_URL} for _ in range(images))
    return [{"role": "user", "content": content}]


# ------------------------------------------------------------ the router --


def test_the_vision_model_is_served_by_the_router_under_its_public_id_only(api, sidecar, main_engine):
    fake = sidecar(FakeSidecar())
    response = api.post(
        "/v1/responses",
        json={"model": "techsara-8b-vision", "input": "Describe the sky.", "max_output_tokens": 64},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "techsara-8b-vision"
    assert body["output"][0]["content"][0]["text"] == "Hello there"
    assert body["usage"] == {"input_tokens": 21, "output_tokens": 2, "total_tokens": 23}
    assert body["max_output_tokens"] == 64
    # The main model was never asked, and the router was asked for ITS model.
    assert main_engine == []
    # The read timeout is the 1,800 s silence FALLBACK (no-timeout design,
    # 2026-09-13), a setting and never a request value (F048).
    assert fake.clients == [(settings.router_base_url, engines.sidecar_read_timeout_s())]
    assert engines.sidecar_read_timeout_s() == 1800.0
    sent = fake.requests[0]
    assert sent["model"] == settings.router_model
    assert sent["max_tokens"] == 64 and sent["stream"] is True
    assert sent["stream_options"] == {"include_usage": True}
    # Always sent, so the checkpoint's generation_config (0.7) never applies.
    assert sent["temperature"] == 0.2
    assert sent["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    for leak in (settings.router_model, "vllm-router", "30002"):
        assert leak not in response.text


def test_a_vision_request_beyond_the_public_window_is_clamped_and_says_so(api, sidecar):
    fake = sidecar(FakeSidecar())
    # 20,000 bytes: inside the 24,320-token input ceiling on the byte bound,
    # ~6,700 estimated tokens — so 20,000 output tokens do not fit the window.
    long_input = "word " * 4_000

    response = api.post(
        "/v1/responses",
        json={"model": "techsara-8b-vision", "input": long_input, "max_output_tokens": 20_000},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    applied = response.json()["max_output_tokens"]
    assert 1 <= applied < 20_000
    # The engine was sent exactly the applied ceiling.
    assert fake.requests[0]["max_tokens"] == applied


def test_the_router_streams_the_documented_lifecycle(api, sidecar):
    sidecar(FakeSidecar(finish="length"))
    response = api.post(
        "/v1/responses",
        json={"model": "techsara-8b-vision", "input": "hi", "stream": True, "max_output_tokens": 5},
        headers=_auth(),
    )

    records = events.parse_frames(response.text)
    names = [record["event"] for record in records]
    assert names[0] == "response.created" and names[-1] == "response.completed"
    # response.queued describes the MAIN engine's recovery; never for a sidecar.
    assert "response.queued" not in names
    final = records[-1]["data"]["response"]
    assert final["max_output_tokens"] == 5
    assert final["incomplete_details"] == {"reason": "max_output_tokens"}


def test_the_chat_completions_dialect_reaches_the_router_too(api, sidecar):
    fake = sidecar(FakeSidecar())
    response = api.post(
        "/v1/chat/completions",
        json={
            "model": "techsara-8b-vision",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ]}],
            "max_completion_tokens": 32,
        },
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "Hello there"
    assert response.json()["max_output_tokens"] == 32
    parts = fake.requests[0]["messages"][0]["content"]
    assert parts == [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": DATA_URL}},
    ]


# ------------------------------------------------------------------- OCR --


def test_ocr_with_no_text_is_sent_the_one_prompt_that_reads_correctly(api, sidecar):
    fake = sidecar(FakeSidecar(pieces=["INVOICE 42"]))
    response = api.post(
        "/v1/responses", json={"model": "techsara-ocr", "input": _image_input()}, headers=_auth()
    )

    assert response.status_code == 200, response.text
    sent = fake.requests[0]
    assert sent["model"] == settings.ocr_model
    assert sent["temperature"] == 0.0
    assert sent["messages"][-1]["content"][-1] == {"type": "text", "text": "OCR"}
    assert "extra_body" not in sent
    # The window is the engine's own 8,192, so the image is counted at the
    # 2,048-token bound and the output clamped below the default.
    assert sent["max_tokens"] < 8192 - 2048
    assert response.json()["max_output_tokens"] == sent["max_tokens"]


def test_ocr_respects_text_the_caller_sent(api, sidecar):
    fake = sidecar(FakeSidecar())
    api.post(
        "/v1/responses",
        json={"model": "techsara-ocr", "input": _image_input("Transcribe the table only.")},
        headers=_auth(),
    )
    texts = [p for p in fake.requests[0]["messages"][-1]["content"] if p["type"] == "text"]
    assert texts == [{"type": "text", "text": "Transcribe the table only."}]


@pytest.mark.parametrize("images", [0, 2])
def test_ocr_reads_exactly_one_image_per_request(api, sidecar, images):
    fake = sidecar(FakeSidecar())
    body_input = _image_input("read", images=images) if images else "no image here"
    response = api.post(
        "/v1/responses", json={"model": "techsara-ocr", "input": body_input}, headers=_auth()
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "input"
    assert fake.requests == []


def test_a_model_on_an_endpoint_it_does_not_serve_is_a_400_naming_model(api, sidecar):
    response = api.post(
        "/v1/responses", json={"model": "techsara-embed", "input": "hi"}, headers=_auth()
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "model"
    assert response.json()["error"]["message"] == "The model `techsara-embed` does not support /v1/responses."


# -------------------------------------------------------- engine failures --


def test_an_engine_that_cannot_read_an_image_is_a_400_with_a_fixed_sentence(api, sidecar):
    request = httpx.Request("POST", "http://vllm-ocr:30004/v1/chat/completions")
    refusal = openai.BadRequestError(
        "cannot identify image file at /tmp/x from 10.0.0.9",
        response=httpx.Response(400, request=request),
        body={"message": "cannot identify image file"},
    )
    sidecar(FakeSidecar(open_error=refusal))

    response = api.post(
        "/v1/responses", json={"model": "techsara-ocr", "input": _image_input()}, headers=_auth()
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "An image in this request could not be read by the model."
    assert "10.0.0.9" not in response.text and "/tmp/x" not in response.text


def test_an_unreachable_router_is_a_retry_safe_503_and_never_waits_for_a_restart(api, sidecar):
    refusal = openai.APIConnectionError(request=httpx.Request("POST", "http://vllm-router:30002/v1"))
    sidecar(FakeSidecar(open_error=refusal))

    response = api.post(
        "/v1/responses", json={"model": "techsara-8b-vision", "input": "hi"}, headers=_auth()
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_sidecar_path_refuses_to_become_a_way_around_the_main_engines_lanes(monkeypatch, sidecar):
    fake = sidecar(FakeSidecar())
    target = engines.EngineTarget(
        key="router", base_url=settings.openai_base_url, model=settings.router_model
    )

    async def scenario():
        with pytest.raises(errors.ApiError) as refused:
            async for _ in engines.stream_chat(
                target, [{"role": "user", "content": "x"}], max_tokens=1, temperature=0
            ):
                pass
        return refused.value

    assert asyncio.run(scenario()).code == "model_unavailable"
    assert fake.requests == []


def test_a_sidecar_generation_has_no_wall_clock_and_runs_to_its_end(sidecar):
    """2026-09-13 (no-timeout design): the old 0.12 s wall clock cut this
    answer and failed it as `timeout`. Now nothing but the engine ends it —
    even a legacy spec that still carries `wall_clock_s` is not cut."""
    sidecar(FakeSidecar(pieces=["a", "b", "c", "d"], delay=0.05))
    spec = streaming.GenerationSpec(
        response_id="resp_wall",
        model="techsara-8b-vision",
        messages=[{"role": "user", "content": "go"}],
        max_tokens=16,
        temperature=0.2,
        created_at=1,
        engine="router",
        wall_clock_s=0.12,
    )

    outcome = asyncio.run(streaming.run_to_completion(spec))

    assert outcome.status == "completed", outcome.error
    assert outcome.text == "abcd"


def test_a_sidecar_continuation_asks_the_engine_to_extend_the_assistant_text(sidecar):
    """A durable router run resumes by continuation: the partial answer is the
    last message and vLLM is told to continue it, not to start a new turn."""
    fake = sidecar(FakeSidecar(pieces=[" 5 6"]))
    spec = streaming.GenerationSpec(
        response_id="resp_cont",
        model="techsara-8b-vision",
        messages=[{"role": "user", "content": "count"}],
        max_tokens=16,
        temperature=0.0,
        created_at=1,
        engine="router",
    )
    messages = [{"role": "user", "content": "count"}, {"role": "assistant", "content": "1 2 3 4"}]

    async def scenario():
        generation = streaming.Generation(
            spec, messages=messages, max_tokens=8, continue_final_message=True
        )
        texts = [chunk.text async for chunk in generation.stream() if chunk.kind == "token"]
        await generation.aclose()
        return texts

    assert asyncio.run(scenario()) == [" 5 6"]
    sent = fake.requests[0]
    assert sent["messages"][-1] == {"role": "assistant", "content": "1 2 3 4"}
    assert sent["max_tokens"] == 8
    assert sent["extra_body"]["continue_final_message"] is True
    assert sent["extra_body"]["add_generation_prompt"] is False


def test_the_capacity_gate_refuses_a_sidecar_stream_before_its_status_line(api, sidecar, monkeypatch, platform):
    from app.publicapi import capacity
    from app.publicapi import router as public_router

    fake = sidecar(FakeSidecar())
    set_setting(monkeypatch, "PUBLIC_API_GATE_WAIT_S", "0.05")
    set_setting(monkeypatch, "PUBLIC_API_ROUTER_MAX_CONCURRENT", "1")
    if hasattr(public_router, "_patient_gate"):
        # No-timeout design, once T3's router waits through the patient gate:
        # a full sidecar gate is a wait IN THE BODY, never a 503 — the stream
        # starts, waits while the gate is held, and completes once it frees.
        import threading

        held = api.portal.wrap_async_context_manager(capacity.hold("router", wait_s=1))
        held.__enter__()
        releaser = threading.Timer(0.6, lambda: held.__exit__(None, None, None))
        releaser.start()
        try:
            response = api.post(
                "/v1/responses",
                json={"model": "techsara-8b-vision", "input": "hi", "stream": True},
                headers=_auth(),
            )
        finally:
            releaser.join(5)
        assert response.status_code == 200
        assert "response.completed" in response.text
        assert len(fake.requests) == 1
        return

    # The gate lives on the app's own event loop, so it is held from there.
    with api.portal.wrap_async_context_manager(capacity.hold("router", wait_s=1)):
        response = api.post(
            "/v1/responses",
            json={"model": "techsara-8b-vision", "input": "hi", "stream": True},
            headers=_auth(),
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "model_unavailable"
    assert response.headers["Retry-After"] == "5"
    assert fake.requests == []
    # Refused before anything durable was written.
    assert db.list_api_responses(platform["project"]["id"]) == []
