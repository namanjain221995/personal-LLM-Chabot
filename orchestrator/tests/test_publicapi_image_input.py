"""Image input on `/v1/responses` and `/v1/chat/completions` (2026-09-13).

The rules, each pinned against a stub engine that records what it was given:

* data: URLs ONLY. vLLM fetches an http(s) image URL itself, from inside the
  cluster network, with no media-domain allowlist — so a caller's URL must
  never reach an engine (SSRF). Proved on both dialects;
* the declared type must match the bytes, and each image is size-bounded;
* images only on user turns, only for models that see, within each model's
  per-request count;
* the body may be up to 20 MiB because of images, but the TEXT inside it is
  still held to CONTRACT §12's mebibyte.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Dict, List

import pytest

from app import llm
from app.config import settings
from app.publicapi import engines, models
from tests.test_publicapi_routes import TOKENS, _auth, _pepper, api, platform  # noqa: F401

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10" + b"\x00" * 40
PNG = base64.b64encode(PNG_BYTES).decode()
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    async def no_probe(engine):
        return None

    monkeypatch.setattr(engines, "served_window", no_probe)


@pytest.fixture()
def engine(monkeypatch):
    seen: List[Any] = []

    def fake(messages, **kwargs):
        seen.append(messages)

        async def run():
            yield ("token", "a picture")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    return seen


def _responses(api, content, **body):
    return api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": [{"role": "user", "content": content}], **body},
        headers=_auth(),
    )


def test_an_inline_image_reaches_the_main_model_as_an_image_part(api, engine):
    response = _responses(
        api,
        [
            {"type": "input_text", "text": "What is in this picture?"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}", "detail": "auto"},
        ],
    )

    assert response.status_code == 200, response.text
    assert engine[0] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this picture?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            ],
        }
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.68:30004/v1/models",
        "https://example.com/cat.png",
        "file:///etc/passwd",
        "//vllm-router:30002/",
    ],
)
def test_a_remote_image_url_never_reaches_any_engine_on_either_dialect(api, engine, url):
    responses = _responses(api, [{"type": "input_image", "image_url": url}])
    chat = api.post(
        "/v1/chat/completions",
        json={
            "model": "techsara-35b",
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}],
        },
        headers=_auth(),
    )

    for response in (responses, chat):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request_error"
        # Nothing of the URL is echoed back either.
        assert "192.168" not in response.text and "passwd" not in response.text
    assert engine == []


def test_an_image_whose_bytes_are_not_its_declared_type_is_refused(api, engine):
    response = _responses(api, [{"type": "input_image", "image_url": f"data:image/png;base64,{JPEG}"}])
    assert response.status_code == 400
    assert "declared type" in response.json()["error"]["message"]
    assert engine == []


def test_an_image_larger_than_the_per_image_limit_is_refused(api, engine, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_image_bytes", len(PNG_BYTES) - 1, raising=False)
    response = _responses(api, [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}])
    assert response.status_code == 400
    assert engine == []


def test_a_detail_the_engines_cannot_honour_is_refused_rather_than_ignored(api, engine):
    response = _responses(
        api, [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}", "detail": "high"}]
    )
    assert response.status_code == 400
    assert engine == []


def test_an_image_in_an_assistant_turn_is_refused(api, engine):
    response = api.post(
        "/v1/responses",
        json={
            "model": "techsara-35b",
            "input": [
                {"role": "assistant", "content": [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}]},
                {"role": "user", "content": "and now?"},
            ],
        },
        headers=_auth(),
    )
    assert response.status_code == 400
    assert engine == []


def test_an_image_for_a_model_that_cannot_see_is_refused(api, engine, monkeypatch):
    import dataclasses

    blind = dataclasses.replace(settings.main_capabilities, supports_vision=False)
    monkeypatch.setattr(settings, "main_capabilities", blind)
    response = _responses(api, [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}])
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "The model `techsara-35b` does not accept image input."
    assert engine == []


def test_more_images_than_the_model_takes_is_refused(api, engine):
    parts = [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}] * 17
    response = _responses(api, parts)
    assert response.status_code == 400
    assert "at most 16 images" in response.json()["error"]["message"]
    assert engine == []


def test_an_unknown_part_type_is_refused_without_echoing_what_was_sent(api, engine):
    response = _responses(api, [{"type": "input_audio_10.0.0.7", "data": "x"}])
    assert response.status_code == 400
    assert "10.0.0.7" not in response.text
    assert engine == []


def test_a_body_larger_than_the_text_rule_is_accepted_when_the_excess_is_image(api, engine, monkeypatch):
    monkeypatch.setattr("app.publicapi.models.max_body_bytes", lambda: 4096)
    big_png = base64.b64encode(PNG_BYTES + b"\x00" * 8000).decode()
    response = _responses(
        api,
        [
            {"type": "input_text", "text": "describe"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{big_png}"},
        ],
    )
    assert response.status_code == 200, response.text


def test_text_beyond_the_mebibyte_rule_is_a_413_even_inside_the_larger_media_body(api, engine, monkeypatch):
    monkeypatch.setattr("app.publicapi.models.max_body_bytes", lambda: 4096)
    response = _responses(api, [{"type": "input_text", "text": "x" * 5000}])
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "Images do not count" in response.json()["error"]["message"]
    assert engine == []


def test_a_body_over_the_media_cap_is_refused_before_it_is_parsed(api, engine, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_media_body_bytes", 3000, raising=False)
    monkeypatch.setattr("app.publicapi.models.max_body_bytes", lambda: 2000)
    response = _responses(api, [{"type": "input_text", "text": "y" * 4000}])
    assert response.status_code == 413
    assert "3000 byte limit" in response.json()["error"]["message"]


def test_a_large_body_is_decoded_and_validated_off_the_event_loop(api, engine, monkeypatch):
    threads = []
    real = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        threads.append(getattr(fn, "__name__", getattr(getattr(fn, "func", None), "__name__", "")))
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    big_png = base64.b64encode(PNG_BYTES + b"\x00" * 100_000).decode()
    response = _responses(api, [{"type": "input_image", "image_url": f"data:image/png;base64,{big_png}"}])

    assert response.status_code == 200, response.text
    assert "loads" in threads and "_parse_responses" in threads


def test_the_chat_dialect_accepts_max_completion_tokens_and_refuses_both_spellings(api, engine):
    one = api.post(
        "/v1/chat/completions",
        json={"model": "techsara-35b", "messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 12},
        headers=_auth(),
    )
    both = api.post(
        "/v1/chat/completions",
        json={
            "model": "techsara-35b",
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 12,
            "max_tokens": 12,
        },
        headers=_auth(),
    )

    assert one.status_code == 200 and one.json()["max_output_tokens"] == 12
    assert both.status_code == 400 and both.json()["error"]["param"] == "max_completion_tokens"


def test_a_chat_image_part_with_an_extra_field_is_refused(api, engine):
    response = api.post(
        "/v1/chat/completions",
        json={
            "model": "techsara-35b",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}", "fetch": "yes"}},
            ]}],
        },
        headers=_auth(),
    )
    assert response.status_code == 400
    assert engine == []


def test_text_only_parts_are_sent_to_the_engine_as_one_string(api, engine):
    response = api.post(
        "/v1/responses",
        json={
            "model": "techsara-35b",
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": "Be brief."}]},
                {"role": "user", "content": [{"type": "input_text", "text": "one"}, {"type": "input_text", "text": "two"}]},
            ],
        },
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    # A system turn as parts would be a second system block to Qwen's
    # template, which refuses the request outright.
    assert engine[0] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "one\n\ntwo"},
    ]


def test_the_body_cap_seam_for_the_application_middleware_names_each_routes_cap(monkeypatch):
    monkeypatch.delenv("PUBLIC_API_MAX_MEDIA_BODY_BYTES", raising=False)
    monkeypatch.delenv("PUBLIC_API_MAX_AUDIO_BODY_BYTES", raising=False)
    assert models.body_cap_for("POST", "/v1/responses") == 20 * 1024 * 1024
    assert models.body_cap_for("POST", "/v1/chat/completions/") == 20 * 1024 * 1024
    # 90 MiB of audio and 8 MiB of pooling JSON since 2026-09-14 (CONTRACT §8.4-§8.6).
    assert models.body_cap_for("POST", "/v1/audio/transcriptions") == 94_371_840
    assert models.body_cap_for("POST", "/v1/embeddings") == 8 * 1024 * 1024
    assert models.body_cap_for("GET", "/v1/responses") == models.max_body_bytes()
